# Provider Transforms — Validated Design Against Real r/LocalLLaMA Engines

Binding context: `docs/design/ENGINE_REGISTRY_DESIGN.md §4`. This document **validates and revises** the transform thesis against an audit of the engine/model families that actual local-inference users bring, then specifies the system to fit that reality. It is written to survive a skeptical r/LocalLLaMA infra reader who will immediately point a weird engine at it.

## 0. What the evidence says about the thesis — honest verdict

The thesis — "provider/engine quirks are DATA expressed through a few declarative primitives plus a handful of named built-ins, so a new engine is mostly UI config, not custom code" — **survives, as a direction and as a falsifiable claim, but three of the draft's headline numbers were overclaimed and are corrected here.** After auditing 14 engine/model families against the live code, the honest tally is **~60% pure declarative data (rename / drop / default / value-map / clamp), ~20% named built-ins we ship and review once (not operator code), ~12% protocol-adapter or server-startup-flag territory that is explicitly out of transform scope, and ~8% documented cross-turn limitations we refuse to fake.** The single defensible claim a skeptic will agree with after testing it: **no real engine quirk forces the *operator* to write or import code** — every break resolves to a reviewed primitive, a reviewed built-in we ship, an honestly-named gateway I/O feature, a per-protocol adapter the design names up front, or a documented limitation; the `kind: plugin` arbitrary-code door stays shut. The draft's specific errors, now fixed: (1) reasoning is **not** "four built-ins" — gpt-oss Harmony needs a **fifth, stateful** demux built-in and it is the most common current reasoning+tools model; (2) tool-call extraction on un-flagged servers is **~6 family extractors, not 1**, pushing the built-in count to **~18–20, not ~13**; (3) "buffers ≤1 chunk" undersells streaming `<think>`-splitting, which needs a **delimiter-spanning state machine**, and capability flags must default to **detect-and-normalize, not trust-and-skip** — a pattern the codebase *already* follows at `proxy.py:18936`.

### 0.1 Tally by tier

| Tier | What it is | Share | Code per engine? |
|------|-----------|-------|------------------|
| **Tier 1** | pure rename / drop / default / value-map / clamp — pure data | **~46%** | None (UI form) |
| **Tier 2** | declarative primitive w/ nested path + `when:` guard | **~15%** | None (YAML) |
| **Named built-in** | composed/stateful code we ship + review once, attach by id | **~20%** | Ours, not operator's |
| **Tier 3** | server-startup flag OR novel wire protocol | **~11%** | Out of scope (registry metadata / per-protocol adapter) |
| **Documented limitation** | cross-turn state / engine bug — refused, not faked | **~8%** | Honest gap |

The load-bearing point: **~61% is pure declarative data and ~81% is "config, not custom-per-engine code."** The original "5 primitives + a handful of built-ins" was directionally right and numerically light.

### 0.2 The three hardcoded branches this replaces — verified in source

- `proxy.py:5240` `apply_chat_completions_reasoning_effort()` (dispatched at `:2985`) — reasoning-effort → `/no_think` + `chat_template_kwargs.enable_thinking=false`. Note: it **inserts a system message at index 0** when none exists (`:5283`), and is explicitly "belt-and-suspenders" (`:5267`).
- `proxy.py:2438` `VOICE_MAP` + `:2460` `DEFAULT_VOICE` (applied `:3399`).
- `proxy.py:4862` / `:4879` — inline `/no_think` + `enable_thinking:false` in the background-removal helper.

And two existing facts the design must build *on*, not around:
- `proxy.py:18936` `_normalize_chat_completion_visible_content()` — an **already-shipped, NOT capability-gated, detect-and-normalize** response pass that promotes leaked `<think>` into `reasoning_content` and backfills empty content. The "self-healing" pattern the red-team demands is already how this proxy behaves.
- `proxy.py:19000` `_normalize_chat_completion_sse_chunk()` — JSON-parses each `data:` line **independently** (`:19027`). A `</think>` split across two SSE chunks is **not** caught. This is the concrete proof that streaming reasoning splits need real cross-chunk state, not a per-chunk regex.

---

## 1. THE REVISIONS THE EVIDENCE FORCES

None is a cop-out — each is a concrete, counted category the draft's primitive set failed.

**Revision A — primitives must run on the RESPONSE path.** The most common quirk in the audit is the reasoning-field rename: `reasoning` (vLLM, Ollama, LM Studio) vs `reasoning_content` (SGLang, TabbyAPI, llama.cpp, DeepSeek-R1) on both `message` and `delta`. That is **8+ distinct bullets** and it is a *response* key rename. **Fix: `param-map`, `drop-params`, `value-map`, and `clamp` all carry a `phase: request | response` field.** One response `param-map` per backend collapses ~8 built-in candidates into reusable primitive instances.

**Revision B — primitives must support a `when:` predicate on sibling fields.** At least **11 bullets** are "do X only when Y is present in the same request": drop `response_format` when `tools` present (DeepSeek V3.1); force `stream:false` when `tools` present (LM Studio 0.3.17, Ollama Llama-3 `finish_reason`); drop `logprobs/top_logprobs` when thinking active (DeepSeek hosted 400); inject `parallel_tool_calls:true` only when `tools` non-empty; `enable_thinking=false` only when `response_format` is `json_schema` (llama.cpp grammar-init conflict, #21600). **`when:` is a first-class field on every primitive**, with ops `present`, `absent`, `equals`, `in`, `non_empty`, `gt`, `lt`. It is a guard, not arbitrary logic.

**Revision C — add a sixth primitive: `clamp`.** TabbyAPI rejects `frequency_penalty`/`presence_penalty` < 0 (Pydantic `ge=0`, 422); accepts `temperature` 0..10 where strict OpenAI clients want 0..2; Mistral penalty ranges differ. `value-map` is a discrete lookup and cannot coerce a continuous range. **3 bullets** with no honest home in the original five.

**Revision D (red-team must-fix) — capability flags default to detect-and-normalize, not trust-and-skip.** Operator-asserted `reasoning_parser` / `tool_parser` flags (§4b) are *unprobed assertions*. If mis-set, a capability-*gated-off* normalizer ships raw `<think>` or raw tool text silently. **Response normalizers therefore default to self-healing: they inspect the payload and act when the canonical shape is absent, regardless of the flag.** The flag is an *optimization hint* ("you can skip the scan") and a *router guard* ("don't send `tools` to a backend with no parser"), never a license to emit garbage. This matches the existing un-gated `_normalize_chat_completion_visible_content` at `proxy.py:18936`.

### 1.1 Does any BREAKS-MODEL quirk demand a new *declarative* primitive?

**No.** Every irreducible quirk is one of three things, none of which a *declarative* primitive should pretend to solve:

1. **Stateful streaming rewrite** (buffer/correlate/re-emit across chunks) — Harmony channel demux, Ollama tool-call index demux, two-phase R1 reconstruction, streaming `<think>`-split, Mistral streaming tool-id trim. A primitive is stateless-per-chunk by definition. **Honest answer: a named built-in with an explicit `stateful_stream: true` flag** — we ship a bounded set, not an open door.
2. **Cross-turn conversation state** — DeepSeek V3.x `reasoning_content` passback (depends on whether a *prior* assistant turn had `tool_calls`; the rule **reverses** between R1 and V3/V4). Needs history inspection. **Named built-in or documented limitation.**
3. **Engine-internal bug / server flag** — Mistral `skip_special_tokens` mutual exclusion, KoboldCpp global `thinkformats`, llama.cpp grammar-init crash. **No transform fixes these; they are registry metadata + operator docs** (and one is partially rescued by Revision B, noted honestly in §6).

A `kind: plugin` door would "cover" buckets 1–2 the way `eval()` "covers" parsing. We keep it shut and ship the highest-frequency stateful built-ins ourselves.

---

## 2. THE FINAL PRIMITIVE SET

Six primitives. Each is pure YAML, reviewable without reading Python. Every primitive accepts the shared optional fields `phase` (default `request`) and `when` (default always).

### Shared envelope

```yaml
<id>:
  kind: <primitive>
  phase: request | response          # default request
  when:                              # optional guard; ALL clauses must hold
    - { field: tools, op: non_empty }
    - { field: stream, op: equals, value: true }
  # ...primitive-specific fields...
```

`when` ops: `present`, `absent`, `equals`, `in`, `non_empty`, `gt`, `lt`. Fields use dotted/indexed paths (`response_format.type`, `choices[*].delta.reasoning`, `messages[*].role`).

### 2.1 `value-map` — discrete value lookup on one param

```yaml
voxtral-voices:                      # migrates VOICE_MAP (proxy.py:2438)
  kind: value-map
  param: voice
  map: { alloy: neutral_female, echo: casual_male, fable: cheerful_female }
  default: neutral_female            # was DEFAULT_VOICE (proxy.py:2460)
```
Also: Ollama `response_format json_object → json`; DeepSeek `reasoning_effort=minimal → low` (Ollama rejects `minimal`).

### 2.2 `drop-params` — remove unsupported params (conditional-capable)

```yaml
ollama-unsupported:
  kind: drop-params
  drop: [n, logprobs, top_logprobs, logit_bias, tool_choice, user]

deepseek-thinking-sanitize:          # Revision B
  kind: drop-params
  drop: [logprobs, top_logprobs]     # hard-400 in thinking mode
  when: [{ field: chat_template_kwargs.thinking, op: equals, value: true }]
```
Also: SGLang `suffix`/`best_of`; vLLM `store/metadata/prediction/audio/modalities`. KoboldCpp null-param strip is a *built-in* (`null-param-strip`), because Kobold 422s on `null` rather than ignoring it — a value test, not a key list.

### 2.3 `default-params` — inject defaults if absent (nested-path capable)

```yaml
sglang-separate-reasoning:
  kind: default-params
  set: { extra_body.separate_reasoning: true }   # nested path
  only_if_absent: true

qwen3-nonthinking-sampling:
  kind: default-params
  set: { temperature: 0.7, top_p: 0.8, top_k: 20, min_p: 0 }
  when: [{ field: chat_template_kwargs.enable_thinking, op: equals, value: false }]
```
Also: llama.cpp `cache_prompt:true`, `parallel_tool_calls:true`; DeepSeek-R1 `temperature:0.6`.

### 2.4 `param-map` — rename / move a param across a (possibly nested) path; phase-directional

```yaml
ollama-reasoning-rename:             # Revision A — RESPONSE phase
  kind: param-map
  phase: response
  from: choices[*].delta.reasoning
  to:   choices[*].delta.reasoning_content

vllm-guided-json:
  kind: param-map
  from: response_format.json_schema.schema
  to:   guided_json
```
Also: Ollama native `/api/chat` `max_tokens → options.num_predict`; TabbyAPI streaming `model_name → model`. **Scope boundary (red-team fix):** `param-map` renames or relocates a *scalar at a path*. It does **not** scatter-gather a filtered nested array (e.g. moving image blocks out of `messages[*].content[]` into a sibling `images[]` with a list→string collapse) — that is the `ollama-vision-reshape` built-in (§3), called out so no one mistakes it for a rename.

### 2.5 `message-inject` — prepend/append/modify a system or role message

```yaml
qwen-no-think-soft:                  # the /no_think soft switch
  kind: message-inject
  role: system
  text: "/no_think"
  mode: append-or-insert             # append to a system msg; else insert at front

llama3-tool-role:                    # value substitution on message role
  kind: message-inject
  op: role-rewrite
  match: { role: tool }
  set:   { role: ipython }
  when:  [{ field: _backend.template, op: equals, value: generic }]
```
**Fidelity note:** the current `apply_chat_completions_reasoning_effort` *inserts at index 0* when no system message exists (`proxy.py:5283`); `append-or-insert` reproduces that exactly. Also: DeepSeek-R1-original system→user merge; Llama-3 `tool→ipython` on generic templates.

### 2.6 `clamp` — NEW. Coerce a numeric param into [min, max]

```yaml
tabby-penalty-clamp:
  kind: clamp
  param: frequency_penalty
  min: 0.0                           # TabbyAPI Pydantic ge=0

openai-egress-temp-clamp:
  kind: clamp
  phase: response
  param: temperature
  max: 2.0                           # TabbyAPI 0..10; strict OpenAI 0..2
```
Justifies 3 audit bullets `value-map` cannot express because the domain is continuous.

---

## 3. THE NAMED BUILT-INS — ~18–20, bounded and individually reviewable

A built-in is **reviewed code we ship once and attach by id** — never operator code. Each composes primitives an operator could not safely hand-assemble, or holds bounded state. The draft said "~13"; the honest count after the red-team's tool-extractor and Harmony corrections is **~18–20**.

### 3.1 Reasoning normalization (the headline surface)

| Built-in id | Composes / does | Serves | Stateful? |
|-------------|-----------------|--------|-----------|
| **`qwen3-thinking`** | `param-map`(reasoning_effort→enable_thinking) + `default-params` + `message-inject`(/no_think). Prefers `enable_thinking=false` over the soft switch when the backend honors it. | Qwen3 on vLLM, SGLang, llama.cpp, LM Studio | No |
| **`reasoning-field-normalize`** | response `param-map` that detects `reasoning` OR `reasoning_content` (message + delta) and emits the canonical key. **Detect-and-normalize, not flag-gated (Revision D).** | vLLM, SGLang, Ollama, TabbyAPI, LM Studio, llama.cpp, R1, Gemma | No (per-chunk) |
| **`strip-think-tags`** | When no reasoning parser is active: regex-extract complete `<think>…</think>` (DOTALL; empty-tag safe) from `content`. Variants: Gemma `<|channel|>thought…`, Kimi `◁think▷`. Mirrors existing `proxy.py:18936`. | bare llama.cpp, KoboldCpp w/o parser, Gemma, Kimi K2 | No |
| **`streaming-think-split`** | **Stateful (red-team correction).** A delimiter-spanning state machine that buffers the *minimum tail* (last N bytes that could be a partial `</think>`) across SSE chunks and re-emits reasoning vs content. Replaces the false "≤1 chunk" claim; bounded buffer = longest delimiter, not the whole reasoning phase. `stateful_stream: true`. | bare llama.cpp / Kobold / ooba streaming without `--reasoning-parser` | **Yes** |
| **`harmony-channel-demux`** | **Stateful, NEW (red-team must-fix).** Demuxes gpt-oss Harmony's three interleaved channels (analysis / commentary / final) across SSE chunks; tool calls arrive in `commentary`. No single-delimiter regex demuxes three channels — this is why "reasoning = four built-ins" was wrong. `stateful_stream: true`. | gpt-oss (the most common current reasoning+tools model) on llama.cpp/vLLM/Ollama | **Yes** |
| **`reasoning-stream-normalize`** | **Stateful.** Merge/relabel Ollama's paired reasoning+content chunks; two-phase R1 stream. `stateful_stream: true`. | Ollama, DeepSeek-R1, vLLM reasoning deltas | **Yes** |

The honest headline: **the reasoning surface is six built-ins (two of them stateful streaming demuxers), not four.** That is still "ship-once, review-once code," not per-engine operator code — the thesis holds; the count was light.

### 3.2 Tool-call extraction (the second corrected count)

The draft implied one extractor. **Without `--tool-call-parser`, tool calls arrive as family-specific TEXT** on KoboldCpp / bare llama.cpp / ooba, and the families share no primitive. We ship **~6 extractors** (or 501-block tools on those rigs):

| Built-in id | Parses | Serves |
|-------------|--------|--------|
| **`toolparse-hermes`** | Hermes `<tool_call>{json}</tool_call>` | Qwen/Hermes templates |
| **`toolparse-mistral`** | Mistral `[TOOL_CALLS][{...}]` | Mistral/Mixtral |
| **`toolparse-command-r`** | Command-R `<\|...\|>` action blocks | Cohere Command-R |
| **`toolparse-qwen-xml`** | Qwen XML function blocks | Qwen on generic templates |
| **`gemma4-tool-call-extract`** | Gemma pseudo-JSON (unquoted keys, `<\|"\|>` delim) | Gemma 4 |
| **`toolparse-llama-py`** | Llama Pythonic `[func(arg=val)]` | Llama-3.x |

Plus the structural/streaming tool helpers, family-agnostic:

| Built-in id | Does | Serves | Stateful? |
|-------------|------|--------|-----------|
| **`tool-finish-reason-repair`** | Accumulate `delta.tool_calls`; if final chunk says `finish_reason:stop` but tool calls were seen, override to `tool_calls`. | Ollama (#9632), Mistral/vLLM stream, Kobold fake-stream | **Yes** |
| **`tool-args-stringify`** | If `tool_calls[].function.arguments` is a dict, `json.dumps` it. | llama.cpp (#20198), TabbyAPI inverse | No |
| **`mistral-tool-id-normalize`** | Strip non-alphanumeric; pad/truncate every `tool_call_id` to exactly 9 chars (request + response, nested arrays). | Mistral official + vLLM `--tokenizer_mode mistral` | No |
| **`functions-to-tools`** | Legacy `functions`/`function_call` → `tools`/`tool_choice`; `role:function`→`role:tool` w/ synthetic id. | ooba (hard-rejects legacy), legacy clients | No |

### 3.3 Vision, sampling, history, and engine-specific

| Built-in id | Does | Serves | Stateful? |
|-------------|------|--------|-----------|
| **`ollama-vision-reshape`** | **NEW (red-team must-fix).** Scatter-gather `messages[*].content[].image_url` → sibling `images: [base64]` with list→string collapse. Not a `param-map`. Pairs with `image-url-rehost` (§6) for URL inputs. | Ollama native `/api/chat` (top local engine) | No |
| **`strip-reasoning-from-history`** | **Un-deferred (red-team must-fix): stateless-per-request.** Strip `reasoning_content` / `<think>` from *prior assistant turns* in the outgoing `messages[]`. | gpt-oss (#19703 template crash), SGLang multi-turn, hygiene | No |
| **`deepseek-r1-original-compat`** | Version-gated: system→user merge + `<think>\n` prefill. | DeepSeek-R1 (non-0528) | No |
| **`koboldcpp-penalty-alias`** | Both freq+pres penalties → drop freq (Kobold collapses); else rename freq→pres. | KoboldCpp | No |
| **`null-param-strip`** | Remove keys whose value is `null` (Kobold 422s on null). | KoboldCpp | No |
| **`tabbyapi-greedy-remap`** | Documents / optionally applies temp=0 → {temp:1, top_k:1, top_p:0}. | TabbyAPI (ExLlamaV2) | No |

**Count: ~20 built-ins** (6 reasoning + 10 tool + 4 vision/sampling/history/engine, roughly). Five are `stateful_stream: true`. **`deepseek-reasoning-passback`** is *not* shipped initially — it needs cross-turn memory (bucket 2) and lives in §6.

---

## 4. THE PROTOCOL ADAPTERS (Tier 3) — out of transform scope, by design

Tier 3 is two distinct things the audit conflates; the design must too.

**4a. Novel wire protocols → per-protocol-family adapter (not per engine):**

| Protocol family | Wire shape | Adapter | Engines |
|-----------------|-----------|---------|---------|
| **ComfyUI job-queue** | POST workflow → poll `/history/{id}` → fetch bytes | `comfy` driver (exists: `proxy.py:_comfy_*`, e.g. `:3690`,`:5343`) | ComfyUI image/video |
| **HTTP model load/unload** | `POST /v1/model/load` SSE-progress; admin key | `tabby` lifecycle driver (exists: `proxy.py:1395`) | TabbyAPI |
| **ooba internal model load** | `POST /v1/internal/model/load` | `ooba` driver | text-generation-webui |
| **Anthropic Messages `/v1/messages`** | content-blocks, `x-api-key` | ingress adapter | LM Studio 0.4.1, llama.cpp |
| **OpenAI Responses `/v1/responses`** | stateful `previous_response_id` | `responses_translator.py` (exists: `proxy.py:55`,`:18095`) | LM Studio, Ollama |

A second engine speaking an existing protocol **reuses the adapter** — the design's claim, confirmed. The TabbyAPI model-load path at `proxy.py:1401` (`async for chunk in resp.aiter_bytes()`) is exactly the kind of bespoke streaming machinery that lives in an adapter, not a transform.

**4b. Server-startup flags → registry capability metadata, NOT transforms:**
`--reasoning-parser`, `--tool-call-parser`, `--enable-auto-tool-choice`, `--jinja`, `--tokenizer_mode mistral`, `VLLM_ENFORCE_STRICT_TOOL_CALLING` (~20 bullets). The gateway **cannot inject these per-request** — they are how the server was launched. They become fields on the service record:

```yaml
capabilities:
  reasoning_parser: qwen3            # what the operator launched vLLM with
  tool_parser: hermes
  template: native                   # vs generic — gates llama3-tool-role inject
```

The router reads these to **(a) optimize** (skip a normalizer scan the server already did) and **(b) refuse silently-broken requests** (block `tools` to a backend with no `tool_parser`; return a clean 501, not leaked raw text). **Per Revision D, (a) is an optimization only** — the response normalizer still detect-and-normalizes if the canonical shape is missing, so a *mis-set* flag degrades to a redundant scan, never to garbage egress.

---

## 5. UI — attaching transforms to a service/model

Matches the existing `/admin` left-rail shell (one section at a time; see memory `admin-sidebar-nav`). The common case must be near-zero-click.

### 5.1 Headline flow: add a Qwen model on vLLM

```
Admin → Services → [+ Add backend]
  Step 1  Engine:  ( ) llama.cpp  (•) vLLM  ( ) Ollama  ( ) TabbyAPI  ( ) ComfyUI …
  Step 2  Connection:  base_url [http://10.0.0.50:8000]      [Test ✓ 200, 1 model]
  Step 3  Models:  detected → qwen3-30b-a3b   [map to gateway id: qwen3 ▸]
          ┌─────────────────────────────────────────────────────────────┐
          │ ⚡ Suggested transforms for vLLM + Qwen3:                     │
          │   ☑ qwen3-thinking            (thinking on by default)       │
          │   ☑ reasoning-field-normalize (vLLM emits `reasoning`)       │
          │   ⚠ Server should run --reasoning-parser qwen3  [How? ↗]     │
          │     (if it doesn't, reasoning-field-normalize self-heals;    │
          │      we never ship raw <think> — Revision D)                 │
          └─────────────────────────────────────────────────────────────┘
  Step 4  [Save]  → writes services.local.yml; hot-reload (design §5.3)
```

**Suggestion engine:** a static `(engine, model_family) → [built-in ids]` table ships with the gateway. `engine=vllm` + `qwen3*` auto-checks `qwen3-thinking` + `reasoning-field-normalize` and surfaces the server-flag warning (because §4b says the gateway can't inject it). Operator can uncheck; nothing is silently applied.

### 5.2 The three configuration surfaces

1. **Built-in picker (dropdown + checkbox).** The 90% case. Each id has a one-line description and a "what it does" expander rendering its composed primitives read-only (and `stateful_stream` badge), so a skeptic can audit without reading Python.
2. **Primitive table editor.** `value-map` → key→value grid + `default` row; `drop-params` → tag input; `clamp` → min/max fields. This is where an operator handles a *genuinely new* rename/drop/default/clamp/value-map quirk with no shipped code.
3. **Conditional builder (`when:`).** A "+ Only when…" row → `[field ▾] [op ▾] [value]`, rendering `when: [{field: tools, op: non_empty}]`. Keeps Revision B usable without YAML.

### 5.3 Ordering & conflict visibility

A read-only **"What fires for /chat/completions, in order"** panel (the design's §4.1.1 table, live): global normalizers → service transforms → model transforms, with the merge rule shown (model-level same-id *replaces*) and stateful built-ins flagged as operating on the stream. This is how an operator debugs "my drop-params didn't suppress the inject" without reading code.

---

## 6. WHAT'S HONESTLY NOT COVERED — every BREAKS-MODEL quirk, named

No hand-waving. Each gap is named, bucketed, given a disposition.

### 6a. Shipped as a stateful named built-in
| Quirk | Why declarative fails | Disposition |
|-------|-----------------------|-------------|
| gpt-oss **Harmony** 3-channel demux (analysis/commentary/final; tools in commentary) | Three interleaved channels across SSE chunks; no single-delimiter regex | `harmony-channel-demux` (`stateful_stream`) |
| `<think>` streamed as `delta.content`, delimiter **split across a chunk** | Per-chunk regex can't span the boundary (proven: `proxy.py:19000` parses each `data:` line alone) | `streaming-think-split` (`stateful_stream`) |
| Ollama tool-call `index` always 0 / `finish_reason:stop` for tools | Accumulation across chunks | `tool-finish-reason-repair` |
| Ollama paired reasoning/content; two-phase R1 stream | Chunk N depends on chunk N+k | `reasoning-stream-normalize` |
| Mistral/vLLM streaming `finish_reason` + tool-id trim | Final-chunk decision depends on prior chunks | `tool-finish-reason-repair` + `mistral-tool-id-normalize` |

### 6b. Documented limitation — needs cross-turn state we won't fake
| Quirk | Why | Disposition |
|-------|-----|-------------|
| **DeepSeek V3.x `reasoning_content` passback** (pass back iff *prior* assistant turn had `tool_calls`; rule **reverses** R1 vs V3/V4) | Cross-turn history + version branch | `deepseek-reasoning-passback` — **future**, not SLVP |
| SGLang multi-turn drops `reasoning_content` from history | Same cross-turn surgery | Documented; `strip-reasoning-from-history` mitigates outgoing direction |
| llama.cpp per-request `thinking_budget` | Outside the HTTP body (server flag) | Documented as vLLM/NIM-only |

**Correction vs draft:** `strip-reasoning-from-history` is **stateless-per-request** (it edits the outgoing `messages[]` of *this* request) and therefore **ships now** (§3.3), not deferred. The draft conflated it with the genuinely cross-turn passback rule. Only the passback rule is deferred.

### 6c. Engine bug / server-config — NO transform fixes; registry metadata + docs
- **Mistral `skip_special_tokens` × `--tokenizer_mode mistral` mutual exclusion** — service record `mistral_tokenizer_mode` field; gateway refuses the conflicting tool-call combo with a clean 501.
- **KoboldCpp global `thinkformats` list** (server restart to change) — documented; no per-request override exists.
- **llama.cpp grammar-init 400 on `<think>`+`json_schema` (#21600)** — **partially rescued**: `qwen3-thinking` forces `enable_thinking=false` when `response_format` is `json_schema` (Revision B `when:`), *preventing* the crash. Listed honestly as partial-cover, the one bucket-3 case a conditional primitive rescues.
- **llama.cpp / Gemma peg-array-serialization invalid JSON** — content-size-dependent, needs tool schema at parse time. Engine bug; documented.
- **Ollama image-URL rejection** — needs gateway-side fetch+base64. **`image-url-rehost`: an honestly-named gateway I/O feature, explicitly NOT a transform primitive** (imperative business logic with network I/O). Pairs with `ollama-vision-reshape`.
- **ooba streaming-batch fan-out** (split N / merge N) — documented as unsupported.

**The one-liner for the skeptic:** "If a quirk needs to remember something across HTTP messages, it is a named built-in we ship (`stateful_stream` for within-stream, deferred for cross-turn) or a documented limitation — never operator YAML, never an arbitrary-code plugin." Defensible because the gap set above is enumerated, not gestured at.

---

## 7. MIGRATION — hardcoded branches → registry entries, no behavior change

Strangler-fig, smallest reversible steps. Each step ships behind the registry, **byte-for-byte behavior-preserving**, verified by replaying current responses.

**Step 0 — Transform engine + registry, dark.** Implement the 6 primitives, the `Transform` protocol (`on_request`/`on_response`), the `stateful_stream` built-in contract, and the registry. Attach **nothing**. Add a golden-replay harness diffing request/response bodies through the empty pipeline vs the legacy path. Proves no-op.

**Step 1 — VOICE_MAP → `value-map` (lowest risk).**
```yaml
voxtral-voices: { kind: value-map, param: voice, map: {<VOICE_MAP verbatim>}, default: neutral_female }
```
Attach to the Voxtral TTS service; delete the `VOICE_MAP.get()` at `proxy.py:3399`. Test: every `VOICE_MAP` key + one unmapped voice yields the identical downstream `voice`.

**Step 2 — reasoning-effort → `qwen3-thinking` built-in.** Reproduce `apply_chat_completions_reasoning_effort` (`:5240`) **exactly**: pop+validate `reasoning_effort` against `_REASONING_EFFORT_VALUES` (`:5233`), return the same 400 shape on invalid (`:5260`), suppress only on `{none, minimal}` (`:5237`), set `chat_template_kwargs.enable_thinking=false` via `setdefault`, and **insert `/no_think` at index 0 when no system message exists** (`:5283`) — `message-inject` `append-or-insert` mode. Dispatch at `:2985` becomes "run global request-normalizers" (the pre-routing slot). Test: the existing none/minimal/low/medium/high/xhigh/invalid matrix produces identical bodies and identical 400s.

**Step 3 — inline `/no_think` in background-removal helper.** Replace the hardcoded `/no_think` + `enable_thinking:false` at `proxy.py:4862`/`:4879` by routing that internal call through the same `qwen3-thinking` normalizer. Behavior-identical.

**Step 4 — reasoning-field-normalize on egress (consolidates existing behavior).** The proxy **already** detect-and-normalizes at `proxy.py:18936` and per-chunk at `:19000`. This step ports that logic into `reasoning-field-normalize` (+ `streaming-think-split` for the cross-chunk case the current per-chunk parser misses) with the canonical field set to **exactly what the gateway emits today** (verify first). No silent change for existing clients; the cross-chunk split is the one *new* capability and ships opt-in, replay-gated.

**Step 5 — per-backend drop/default/clamp packs + extractors.** Ship Tier-1 instances (Ollama unsupported-6, KoboldCpp `null-param-strip`, llama.cpp `cache_prompt`) and the tool-family extractors as suggested-on-add config. Additive, individually testable.

**Invariant across all steps:** the Step-0 golden-replay harness gates each merge — any step that changes a current request/response body byte (outside the explicitly-new cross-chunk split in Step 4) fails CI. The hardcoded constants (`VOICE_MAP`, `_REASONING_EFFORT_*`) are deleted only in the step that replaces them, only after replay passes.

---

## Appendix — why this is defensible to a weird engine

A reader pointing a never-audited engine at the gateway lands in exactly one of six places: its quirk is a rename/drop/default/clamp/value-map (**UI table, no code**); a reasoning-field or tool-call-family variant (**one of the ~20 shipped built-ins — six for reasoning incl. two stateful demuxers, six tool-family extractors — already covers it**); a new wire protocol (**they need a per-family adapter, and the design names that up front**); a server-startup flag (**registry metadata, with detect-and-normalize as the safety net so a mis-set flag degrades gracefully, never to garbage**); or stateful/buggy across turns (**a documented gap, not a silent failure**). The falsifiable claim, restated to survive the test: **no real engine quirk requires the operator to write or import code.** The audit's BREAKS-MODEL cases are the stress test; every one is shipped, future-flagged, or documented — with the specific reason it isn't operator-declarable.

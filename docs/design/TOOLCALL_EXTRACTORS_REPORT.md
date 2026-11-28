# Tool-call family extractors — implementation report

**Step 6** of the engine-transforms migration. Implements the six tool-call
family EXTRACTORS named in `ENGINE_QUIRK_FINDINGS.md` (line 41) as response-phase
named built-ins in `gateway/transforms.py`.

## Problem

A backend started WITHOUT a `--tool-call-parser` flag does not lift tool calls
into OpenAI `tool_calls`. The model still emits them — but as plain **text** in
the assistant message `content`, in a model-family-specific format. These six
extractors detect that text and lift it into proper OpenAI `tool_calls`.

## Design invariants

- **Opt-in, never auto-attached.** Each is registered in `REGISTRY` but attached
  only per-backend (like the Step-5 Tier-1 packs). They are NOT in any current
  service's transform list, so they cannot alter any current-config response. A
  backend that *has* a real `--tool-call-parser` already emits proper
  `tool_calls`; these are only for the un-flagged case.
- **Detect-then-extract; pass-through on ambiguity.** A FALSE extract (corrupting
  ordinary prose that merely *looks* like a marker into a broken tool_call) is the
  dangerous, unrecoverable failure. Under-extracting (leaving a genuine call as
  text) is recoverable. So every extractor only acts when the family's exact
  markers are present **and** the body parses; on any malformed markup it returns
  the content **untouched**. This is asserted by a malformed-case test per family.
- **OpenAI output shape.** On extract: `message.tool_calls` is set (each
  `{id, type:"function", function:{name, arguments(JSON string)}}`), the lifted
  text is stripped from `content` (set to `None` if nothing remains), and
  `choice.finish_reason` is set to `"tool_calls"`.
- **Never override engine tool_calls.** If `message.tool_calls` already exists,
  the extractor leaves the choice alone.
- **Non-streaming only.** Only the `message` shape is handled, not streaming
  `delta`. A family marker split across SSE chunks is a separate stateful problem
  (cf. `StreamingThinkSplitTransform`); a naive per-chunk extract would
  false-extract on partial markers. This is documented in code and listed as a
  deferred item below.
- **Pure / deterministic.** Tool-call ids are `call_{index:08d}` — no
  random/uuid — so the transform stays pure and unit-testable.

## The six families

For each: the exact format implemented, the source it was verified against, the
registered id, and the positive/negative/malformed test results. All formats were
verified 2026-06-16.

### 1. Hermes — `hermes2pro-toolcall-extract`

**Format:** `<tool_call>\n{"name": "...", "arguments": {...}}\n</tool_call>` —
JSON object, key order free (the card shows `arguments` first). Text before the
first `<tool_call>` is preserved as visible content. Multiple blocks supported.

**Verified against:** vLLM `hermes_tool_parser.py`
(`tool_call_start_token="<tool_call>"`, `tool_call_end_token="</tool_call>"`,
`tool_call_regex=r"<tool_call>(.*?)</tool_call>..."`) **and** the
`NousResearch/Hermes-2-Pro-Llama-3-8B` model card example output.
**Confidence: verified (primary source).**

**Guard:** because Qwen-XML reuses `<tool_call>` with an XML (`<function=...>`)
body, the Hermes extractor passes through when the first block body starts with
`<function` — so the two never collide.

**Tests:** positive (single + multiple + arguments-first order) ✓; negative
(plain text) ✓; malformed (unterminated tag / bad JSON / missing `name` →
pass-through) ✓.

### 2. Mistral — `mistral-toolcall-extract`

**Format:** `[TOOL_CALLS][{"name": "...", "arguments": {...}}, ...]` — the
`[TOOL_CALLS]` token followed by a JSON **array** of objects. Text before the
token is preserved; text after the array is preserved.

**Verified against:** vLLM `mistral_tool_parser.py`
(`bot_token="[TOOL_CALLS]"`, `tool_call_regex=r"\[{.*}\]"`, splits on the token,
content = part before). **Confidence: verified (primary source).**

> Note: vLLM's newer v11 tokenizer also has a `[TOOL_CALLS]name{args}` (name-then-
> args, no array) form. The findings doc specifies the array form `[TOOL_CALLS][{json}]`,
> which is what un-flagged servers emit via the standard chat template, so the
> array form is what we implement. The name-then-args form is noted as a deferred
> item.

**Tests:** positive (single + leading text + parallel) ✓; negative ✓; malformed
(truncated array / object-not-array → pass-through) ✓.

### 3. Command-R — `commandr-toolcall-extract`

**Format:** ``Action: ```json\n[ {"tool_name": "...", "parameters": {...}} ]\n``` ``
— the literal `Action:` preamble, a ```` ```json ```` fenced block, a JSON
**array**. Keys are **`tool_name`** and **`parameters`** (NOT `name`/`arguments`).
This is the classic `c4ai-command-r-v01` "tool_use" format.

**Verified against:** Cohere *"Prompting Command R and R+"* docs (rendered
template instruction + real model output). **Confidence: verified (primary
source) for the classic format.**

**Flagged ambiguity / not implemented:** the **newer cmd3/cmd4 "melody" format**
(Command-A-Reasoning) is parsed in vLLM by the closed-source `cohere_melody` Rust
package (`cohere_command_tool_parser.py` → `PyFilterOptions().cmd3()/.cmd4()`).
Its exact delimiters are not publicly documented; I did **not** guess them. This
extractor targets only the classic `Action: ```json` format. (A `<|START_ACTION|>`
token variant was investigated and confirmed NOT part of the classic format.)

**Tests:** positive ✓; negative (plain text + bare "Action:" prose with no fence
→ no trigger) ✓; a guard that `name`/`arguments` keys (wrong for Command-R) do
NOT extract ✓; malformed (bad JSON → pass-through) ✓.

### 4. Qwen-XML — `qwen-xml-toolcall-extract`

**Format:**
```
<tool_call>
<function=NAME>
<parameter=KEY>
VALUE
</parameter>
</function>
</tool_call>
```
Parameter values arrive as plain text; each is best-effort JSON-coerced
(`3` → int, `"x"`/bare strings → string). Multiple `<tool_call>` blocks supported;
preamble text preserved.

**Verified literally against:** the `Qwen/Qwen3-Coder-30B-A3B-Instruct`
`tokenizer_config.json` chat template, which prints the exact emit format
(`<tool_call>\n<function=NAME>\n<parameter=KEY>\nVALUE\n</parameter>\n</function>\n</tool_call>`).
**Confidence: verified (primary source — the official chat template).**

**Tests:** positive (int coercion + preamble) ✓; negative (Hermes JSON form inside
`<tool_call>` passes through — disambiguated from family 1; plain text) ✓;
malformed (empty function name → pass-through) ✓.

### 5. Gemma — `gemma-toolcall-extract`

**Format:** a ```` ```tool_code ```` fenced block containing a **Python call
expression** `func(arg=val, ...)`. An optional `print(...)` wrapper is tolerated
(`print(func(...))`). Parsed via `ast`; only keyword args with constant values are
accepted — positional args, nested calls, non-constant values, or non-call bodies
cause pass-through.

**Verified against:** Google / Philipp Schmid Gemma function-calling example
(`tool_code`/`tool_output` convention, literal emitted call
`convert(amount=200000.0, currency="USD", new_currency="EUR")`).
**Confidence: community-convention (consistently documented, including by Google),
NOT a hard spec.**

**Flagged ambiguity:** Gemma has no official tool-call special tokens; the format
is prompt-defined. The dominant convention is ```` ```tool_code ```` + python
call, but ```` ```json ```` and `print(...)` variants genuinely exist. This
extractor implements the dominant `tool_code` + python-call form and tolerates the
`print(...)` wrapper. The brand-new **Gemma 4** format uses real tokens
(`<|tool_call>call:NAME{...}<tool_call|>`) and is a different family — NOT handled
here. A ```` ```json ````-fence fallback was considered but deliberately omitted
to avoid false-extracting ordinary JSON code blocks (noted as deferred).

**Tests:** positive (kwargs + `print()` wrapper) ✓; negative (plain text + a
normal ```` ```python ```` fence does NOT trigger) ✓; malformed (positional args /
non-call body `x = 1 + 2` → pass-through) ✓.

### 6. Llama-3 — `llama3-toolcall-extract`

**Format:** `<|python_tag|>{"name": "...", "parameters": {...}}` — the
`<|python_tag|>` token followed by a JSON object using **`parameters` OR
`arguments`**. Multiple calls separated by `; `. Preamble text preserved.

**Verified against:** vLLM `llama_tool_parser.py`
(`bot_token="<|python_tag|>"`; the parser explicitly accepts either `parameters`
or `arguments`; multiple separated by `"; "`). **Confidence: verified (primary
source).**

**Guard:** requires the `<|python_tag|>` marker. A bare JSON object without the
tag is too risky to claim as a tool call, so it passes through (asserted by test).

**Tests:** positive (`parameters` key + `arguments` key + semicolon-separated
multiple) ✓; negative (plain text + bare JSON without tag → pass-through) ✓;
malformed (truncated JSON → pass-through) ✓.

## Test results

`tests/test_tool_extractors.py` — **66 passed** (0.06s). Per family: registered +
positive(s) + negative(s) + malformed pass-through, plus a shared-guard class:

- `test_clean_response_untouched` (×6) — a normal answer with no markers is
  untouched (the non-alteration guard).
- `test_existing_tool_calls_not_overridden` (×6) — engine-produced `tool_calls`
  are never touched.
- `test_request_phase_is_noop` (×6) — all are response-only.
- `test_non_chat_payload_untouched` (×6) — a payload with no `choices` (e.g.
  embeddings) is untouched.
- `test_extractors_not_in_default_pipeline` — running with an empty id list never
  extracts (registration alone does not attach).

## Full-suite count

`pytest -q`: **1969 passed, 1 skipped, 375 subtests passed, 6 failed.**

The 6 failures are **pre-existing and unrelated** to this change. They are in
`tests/test_discovery_metadata.py` and `tests/test_protocol_route_aliases.py`
(unmapped paths return **401 "Missing API key." instead of 404**), a consequence
of the prior auth commit `e46767c (auth: optional LAN bypass, ?api_key=, and
per-device path tokens)`. **Verified pre-existing:** with `gateway/transforms.py`
stashed out, the same tests still fail identically (401 != 404). No test touches
`transforms.py`; this change introduces **zero regressions** (1906 baseline + 66
new = the transforms surface stays green; the 6 failures sit entirely in the
auth/routing surface and predate this lane's work).

## Deferred / not done cleanly

- **Streaming (`delta`) extraction** — intentionally not implemented. Reassembling
  a family marker split across SSE chunks needs a stateful per-request buffer (the
  `StreamingThinkSplit` pattern); a naive per-chunk extract would false-extract on
  partial markers. Non-streaming `message` extraction is complete.
- **Command-R cmd3/cmd4 "melody" format** — closed-source (`cohere_melody`),
  exact delimiters not publicly documented; not guessed. Only the classic
  `Action: ```json` format is handled.
- **Mistral v11 name-then-args form** (`[TOOL_CALLS]name{args}`) — only the
  findings-specified array form `[TOOL_CALLS][{json}]` is handled.
- **Gemma `json`-fence variant** — deliberately omitted to avoid false-extracting
  ordinary JSON code blocks; only the dominant `tool_code` + python-call form
  (with `print()` wrapper) is handled. Gemma 4's token format is a separate family.
- **Argument type-coercion via the tool schema** — vLLM's Qwen/MiniCPM parsers
  coerce param values using the request's declared tool `properties[].type`. These
  extractors do not see the request tools at response time, so Qwen-XML values are
  best-effort JSON-coerced (number/bool/object) with a string fallback. Adequate
  for the common case; schema-aware coercion is a possible future refinement.

## Wiring note

These are registered built-ins only. The per-backend attach UI/config plumbing
(surfacing them as opt-in transforms when adding an un-flagged backend) is the
same wiring path as the Step-5 Tier-1 packs and is out of scope for this step.

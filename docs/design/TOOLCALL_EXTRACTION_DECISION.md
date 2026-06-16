# Tool-call extraction — keep/cut decision (primary-source research)

**Date:** 2026-06-16. **Verdict: KEEP** the six tool-call extractors as a
conditional, opt-in compensating control. Evidence-backed against the backends
r/LocalLLaMA / r/selfhosted users actually run — NOT against any single stack.

## Why this memo exists

The extractors (gateway/transforms.py, committed 15e7658) lift plain-TEXT tool
calls (Hermes/Mistral/Llama/Qwen-XML/Gemma/Command-R formats) out of assistant
`content` into proper OpenAI `tool_calls`. They only matter when a backend does
NOT lift tool calls itself. The question: do real 2026 backends lift by default
(→ cut as redundant) or do a meaningful share pass raw text (→ keep as a real
gateway responsibility)? Researched primary sources; the answer is KEEP.

## The structural split (primary-source verified)

| Backend | Default tool-call behavior | Flag needed | Source |
|---|---|---|---|
| **Ollama** | LIFTS structured tool_calls | none | ollama tool-support blog (Jul 2024) |
| **LM Studio** | LIFTS | none (since v0.3.5, Oct 2024) | LM Studio API changelog |
| **TGI** | LIFTS (via outlines) | none | HF TGI Messages API docs |
| **TabbyAPI/ExLlamaV2** | LIFTS but args as DICT not JSON string | none (needs args-stringify normalize) | tabby chat_completion.py + prior audit |
| **vLLM** | RAW TEXT in content | `--tool-call-parser <fam>` + `--enable-auto-tool-choice` | docs.vllm.ai/features/tool_calling |
| **llama.cpp / llama-server** | RAW TEXT | `--jinja` (not in quickstart) | ggml-org/llama.cpp function-calling.md |
| **SGLang** | RAW TEXT | `--tool-call-parser <name>` | docs.sglang.io tool_parser |
| **llama-cpp-python server** | RAW TEXT | `--chat_format chatml-function-calling`/functionary | llama-cpp-python README |
| **KoboldCpp** | RAW TEXT (no parser exists) | none available | issue #1448 (Mar 2025) |

**The crux:** the performance tier r/LocalLLaMA runs — vLLM and SGLang — leaves
tool calls as RAW TEXT unless a flag is set that NO default Docker image or
quickstart sets. `vllm serve <model>` (simplest command) → broken
tool_choice="auto". llama.cpp without `--jinja` (not prominent in quickstart) →
same. KoboldCpp → no fix exists at all. A gateway for heterogeneous,
imperfectly-configured backends has a real, frequent job here. NOT speculative.

## Build vs vendor vs library

- **No standalone PyPI library** does cross-family tool-call text extraction.
  instructor / outlines are constrained-GENERATION (wrong layer — they force
  valid output at generation time; a proxy receives already-generated text).
- **vLLM's / SGLang's parsers** (the canonical implementations) are tightly
  coupled to their token-by-token STREAMING internals (ToolParserManager, vLLM
  delta types). A proxy operates on ASSEMBLED content strings / upstream SSE
  chunks — a different abstraction layer. Vendoring would mean a heavyweight
  vLLM dependency or a substantial refactor. Apache-2.0, so legally fine, but
  architecturally wrong for a proxy.
- **Decision: keep the hand-rolled six.** Right abstraction (proxy layer), zero
  heavy deps, ~30-60 lines each, no risk from upstream API churn. Revisit only
  if a clean standalone library emerges.

## Required scoping corrections (from the research)

1. **Conditional no-op:** an extractor must fire ONLY when `tool_calls` is
   absent/empty AND the content matches its family pattern — so it NEVER touches
   a correctly-configured Ollama/LM Studio/TGI response. (Already opt-in/never-
   auto-attached; add the tool_calls-empty guard if not present.)
2. **Per-backend opt-in:** gate by config (e.g. `tool_text_extraction: hermes`)
   so creative-writing backends don't false-extract prose that looks like a
   marker. (Aligns with the false-extract-is-the-dangerous-failure principle.)
3. **No emerging standard rescues this:** the OpenAI `tool_calls` SHAPE is the
   de facto norm, but the ecosystem has NOT converged on "server always lifts."
   The raw-inference backends (vLLM/SGLang/llama.cpp) deliberately keep parsing
   opt-in for performance. The gateway-as-compensating-parser role persists for
   2-3+ years.

## Adjacent real gaps the research surfaced (NOT extraction — normalization)

- **Ollama streaming finish_reason bug:** returns `stop` not `tool_calls` on the
  final SSE chunk when tool calls are present (issues #9632, #12557, through
  ~0.12.3). A `finish_reason` repair transform — a named built-in, not an
  extractor.
- **TabbyAPI args-as-dict:** `tool_calls[].function.arguments` returned as a
  Python dict rather than the spec-required JSON string. An args-stringify
  normalize transform. (Tabby is one of Tim's live backends — this one is real
  for his stack too.)

## Confidence

High on the verdict (vLLM/llama.cpp/SGLang/Ollama/LM Studio/TGI behaviors are
primary-source confirmed). Medium on KoboldCpp specifics (issue #1448 is
slightly ambiguous; ~80%) and the TabbyAPI args-as-dict detail (~70%, from prior
audit not re-verified against tabby source this pass). What couldn't be
confirmed: whether a recent KoboldCpp release silently added lifting; whether
a brand-new standalone library appeared in the last ~3 months.

## Process note (for the orchestrator's own learning)

This decision should have been researched BEFORE the extractors were built, not
after. The build was triggered by a design doc that listed them without first
validating the need against real-backend defaults. Tim caught it with two
questions ("are these already available?" / "are we only building for vLLM?").
The honest test for any engine-quirk built-in: does a POPULAR backend exhibit
this quirk by DEFAULT for a real user? Validate that against primary sources
before building, not after. The extractors survived the test — but the test
should precede the build.

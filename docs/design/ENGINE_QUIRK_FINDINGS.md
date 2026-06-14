# Engine quirk findings — evidence base

Research evidence (2026-06-13) from a 18-agent audit of the inference engines and model
families r/LocalLLaMA / r/selfhosted users actually bring to an OpenAI-compatible gateway.
Durable institutional knowledge: feeds the transforms design (ENGINE_TRANSFORMS_DESIGN.md),
Step 5 implementation, model-compatibility docs, and engine-support decisions.

Source: workflow wf_c6f0cf37-1fc; per-engine raw audits in that run's subagent transcripts.

## Engines triaged (most common first)

- Ollama
- llama.cpp / llama-server
- vLLM
- SGLang
- LM Studio
- TabbyAPI (ExLlamaV2/V3)
- text-generation-webui (oobabooga)
- KoboldCpp
- TGI (Text Generation Inference, HuggingFace)
- mlx_lm / mlx-server (Apple Silicon)
- Aphrodite Engine

## Thesis verdict

VALIDATED WITH MANDATORY REVISIONS. The "5 primitives + a handful of built-ins" thesis survives contact with the real engines, but ONLY after three structural corrections the original design did not make: (1) primitives MUST run on the RESPONSE path, not just the request path — the single largest category of real quirks (reasoning-field renames across vLLM/SGLang/Ollama/TabbyAPI/LM Studio) is a response-side key rename and is unhandleable by a request-only model; (2) primitives MUST support a conditional `when:` predicate keyed on OTHER fields in the same request (tools-present, stream-true, response_format-present, model-family) — at least 11 real quirks are "drop/inject X only when Y", which the unconditional draft primitives cannot express; (3) a sixth primitive, `clamp` (numeric range coercion), is justified by Mistral/TabbyAPI penalty-range and temperature-range deviations that value-map cannot express. With those three changes the coverage holds: ~46% pure Tier-1 mapping (UI form, zero code), ~16% declarative Tier-2 primitive, ~17% named built-in (reviewed code we ship), ~12% Tier-3 protocol/server-config (out of transform scope by design), ~9% genuine BREAKS-MODEL. The BREAKS-MODEL set is real and bounded: it is dominated by STATEFUL STREAMING REWRITES (buffer N SSE chunks, correlate, re-emit) and CROSS-TURN MESSAGE STATE (DeepSeek reasoning_content passback rule). These are honestly NOT declarable — they need either a named built-in with a stateful-stream capability flag (which we ship for the 3 highest-value cases) or a documented limitation. The thesis's core promise — "adding a Qwen-on-vLLM is UI config, not code" — is TRUE: that exact case is one auto-suggested built-in (qwen3-thinking) plus zero custom code.

## Tier tally (≈290 audited quirk-bullets, 14 engine/model families)

Across ~290 audited quirk-bullets spanning 14 engine/model families (Ollama, llama.cpp/llama-cpp-python, KoboldCpp, vLLM, SGLang, LM Studio, TabbyAPI, text-generation-webui, Qwen3, DeepSeek-R1, Mistral/Mixtral, Llama 3.x, Gemma 4, DeepSeek-V3.x): Tier 1 (pure mapping / drop / default — UI form, ZERO code) ≈ 46% (~133); Tier 2 (declarative primitive incl. nested + conditional) ≈ 16% (~47); Named built-in (reviewed composed code we ship) ≈ 17% (~49); Tier 3 (server-startup flag OR novel wire protocol — explicitly out of transform scope) ≈ 12% (~35); BREAKS-MODEL (stateful stream / cross-turn state / engine bug — not declarable) ≈ 9% (~26). Headline: 62% (Tier1+Tier2) is pure declarative data; add named built-ins and 79% is "config, not custom-per-engine code." The remaining 21% splits into 12% legitimately-out-of-scope (server flags + protocol adapters) and 9% honest gaps, of which we ship built-ins for the 3 highest-frequency (stateful reasoning-stream normalize, finish_reason repair, mistral-tool-id) and document the rest.

BREAKS-MODEL count: 26

## Red-team verdict

SOUND-WITH-NAMED-CAVEATS. The falsifiable claim - no quirk forces OPERATOR code - survives: every break is a reviewed built-in, a new declarative primitive, an honestly-carved gateway I/O feature, or a documented cross-turn limitation; kind:plugin stays shut and the doc honestly pre-concedes 26 cases plus three correct revisions. But three numbers are overclaimed: reasoning is NOT four built-ins (Harmony needs a fifth, stateful); tool-call extraction is ~6 not 1 (so ~18-20 total not ~13, uncovered rigs get tools 501-blocked); 'buffers <=1 chunk' undersells live think-splitting. Thesis (mostly UI, rarely code) is defensible as direction and as a falsifiable claim; caveats are count/coverage honesty, not an architectural hole. Confirmed: proxy.py:1401 streams pass-through (stateful built-ins are new machinery), :4728 naive </think> split, :5240 reasoning-effort logic, :5267 belt-and-suspenders pattern."

## Red-team breaks (the quirks that stress the model)

- gpt-oss Harmony 3-channel format (analysis/commentary/final), tools in commentary - needs stateful channel demux across SSE chunks, not a single-delimiter regex.
- Without --tool-call-parser, tools arrive as family TEXT (Hermes, Mistral [TOOL_CALLS], Command-R, Qwen XML, Gemma pseudo-JSON, Llama-py) on Kobold/bare-llama.cpp/ooba.
- Ollama /api/chat wants images:[base64] as a sibling array, not an inline image_url block - a structural content[]->images[] move with list->string collapse.
- Without --reasoning-parser, <think> streams as delta.content across thousands of tokens, delimiter splittable across a chunk.
- Multi-turn reasoning+tools: DeepSeek V3.x passback iff prior turn had tool_calls (reverses R1 vs V3/V4); gpt-oss crashes (#19703) unless reasoning stripped from prior turns; SGLang drops it.
- Operator-asserted reasoning_parser/tool_parser flags gate transforms; not probed, so if mis-set the gateway ships raw <think>/tool text silently. A request-only when: can't catch it.

## Defensible claim (the honest, post-red-team version)

The thesis survives as direction and as a falsifiable claim — "no real engine quirk forces the OPERATOR to write or import code" — but three draft numbers were overclaimed and are now corrected: reasoning is ~6 built-ins not 4 (gpt-oss Harmony needs a fifth stateful demux), tool-call extraction is ~6 family extractors not 1 (total ~18-20 built-ins not ~13), and streaming think-split needs a delimiter-spanning state machine not a ≤1-chunk buffer. Honest tally: ~61% pure declarative data, ~81% config-not-per-engine-code, ~11% protocol-adapter/server-flag, ~8% documented cross-turn limitations. Critically, the codebase ALREADY does the red-team's prescribed detect-and-normalize pattern un-gated at proxy.py:18936, and proxy.py:19000 provably parses each SSE chunk independently — grounding the streaming correction in real code rather than assertion.

## What changed after red-team

- Added harmony-channel-demux as a fifth stateful reasoning built-in; reframed reasoning surface as six built-ins (two stateful streaming demuxers), not four; named gpt-oss as the most common current reasoning+tools model
- Added six tool-call family extractors (Hermes/Mistral/Command-R/Qwen-XML/Gemma/Llama-py) and corrected total built-in count from ~13 to ~18-20; stated uncovered rigs 501-block tools otherwise
- Replaced the false '≤1 chunk buffer' streaming claim with streaming-think-split, a delimiter-spanning state machine with a bounded buffer (longest delimiter, not whole reasoning phase)
- Added Revision D: response normalizers default to detect-and-normalize (self-healing), capability flags are optimization+router-guard hints only — grounded in the existing un-gated proxy.py:18936 behavior
- Un-deferred strip-reasoning-from-history (it is stateless-per-request); kept only the genuinely cross-turn DeepSeek V3.x passback as a documented future limitation, removing the draft's conflation
- Added ollama-vision-reshape built-in and tightened param-map scope to exclude scatter-gather nested-array moves (the Ollama images[] sibling-array reshape)
- Corrected the message-inject fidelity note to match the real insert-at-index-0 behavior at proxy.py:5283, not 'append to first system message'
- Revised the tier tally and headline percentages to the honest ~61% data / ~81% config-not-code / ~8% cross-turn-gaps, replacing the draft's lighter numbers

## Confidence

High on the design's internal honesty and source-groundedness: every cited proxy.py line reference was read and verified (5240, 2438/2460/3399, 4862/4879, 4728, 1401, 18936, 19000-19036), and the two most load-bearing red-team corrections (capability-gating must be detect-and-normalize; per-chunk regex cannot span chunk boundaries) are confirmed by existing code, not just argued. Medium on the absolute built-in count (~18-20) and tier percentages — these derive from the supplied 14-engine audit which I did not independently re-run against live r/LocalLLaMA; the COUNTS are the right order of magnitude and the corrections move them in the honest direction (up, toward more code than the draft claimed), but a fresh audit could shift them ±a few. The architectural claim (UI-config vs named-built-in vs per-protocol-adapter, kind:plugin stays shut) is robust regardless of exact counts.

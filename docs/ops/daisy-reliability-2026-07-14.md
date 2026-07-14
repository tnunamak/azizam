# Daisy reliability handoff — 2026-07-14

## Status

Three incident root causes were confirmed and have targeted fixes; two related hardening fixes are also deployed. The gateway service is active after the gateway changes: read-only service status showed it started at **2026-07-14 05:45:44 CDT**, after `a22482b` and `f3f7e35`. This confirms a restart onto the updated gateway process; it is not a claim of a live end-to-end Bee request after restart.

| Symptom | Proven cause | Owner and landed fix |
| --- | --- | --- |
| After hidden reasoning was force-closed, Bee could keep decoding visible output until cancellation or a natural end. | The visible loop-guard region existed but production code never fed it after a force-close. This was visible generation, not prompt replay or a second hidden-reasoning loop. | **BeeLlama** `006023532`: guard post-force-close visible tokens; on periodic/low-entropy detection, use the normal final-response and slot-release path. Ordinary visible output remains unguarded. |
| Daisy Escape made the UI idle while Bee continued decoding. | The gateway's LiteLLM-to-current-loaded non-stream hop used a non-blocking disconnect check after it had consumed the request body, so it could miss the outer disconnect. | **Vivid Fish gateway** `a22482b`: use the single blocking post-body disconnect watcher, return 499, cancel the read, and close/release the upstream task. |
| Pi compaction retained a tail nominally sized at 20,000 tokens but rebuilt a 51,480-token Qwen request. | Pi 0.80.6's `chars / 4` tail estimate undercounted this tool-heavy context by 2.44×. Pi's recorded cut was correct for that algorithm; the estimate was not. | **Daisy** `5c831c7`: retain the 24,576-token response reserve and lower `keepRecentTokens` to 7,500. |
| A failed Daisy summary could have committed a lossy fallback excerpt. | Compaction preparation and summary failure were not one fail-closed transaction. | **Daisy** `ba2d383`: cancel and preserve the full session on preparation, auth, serialization, or summary failure; use bounded automatic retry backoff and immediate manual retries. |
| A context-exhausted Pi request could look like a successful one-token `length` response. | Pi can clamp output to one token when input consumes the reserved context space. | Public **pi-context-clamp-recovery** `2bd18a7`, installed by Daisy `42a8dab`, records redacted evidence in audit-only mode. No force setting is configured, so it cannot rewrite responses. |

The earlier cache reset was real but separate: a changed short prefix had no valid early hybrid/recurrent checkpoint, so Bee conservatively discarded unusable cache state. It did not re-ingest a 66k prompt and was not caused by the MTP replay work.

## Component contract

- **Gateway:** owns client-disconnect propagation across its internal relay. The fix is intentionally engine-agnostic and belongs in gateway request lifecycle code.
- **BeeLlama:** owns generation finalization and slot release. It detects the post-force-close visible loop; it does not decide Daisy compaction policy.
- **Daisy/Pi:** owns retained-context selection and compaction transaction safety. It preserves Pi's selected boundary unless a future implementation recomputes both the summary and boundary together.
- **Context-clamp extension:** is an isolated, removable Pi workaround. It observes payload/result shape and records redacted metadata; force recovery is explicitly opt-in and currently off.

### Reasoning-budget contract

Gateway policy translates `reasoning_effort` into `thinking_budget_tokens`: `none=0`, `minimal=2048`, `low=4096`, `medium=6144`, `high=8192`, and `xhigh=-1` (unlimited). Only `none` disables thinking. `f3f7e35` corrects stale acceptance expectations so `minimal` remains a positive 2,048-token budget.

Bee's service default may be unlimited, but a request budget overrides it. The incident proved finite overrides reached Bee: low used 4,096 and high used 8,192. Do not diagnose a request as unlimited from the service default alone.

## Verification evidence

- Gateway recorded full-suite result: **2,148 passed, 1 skipped, 387 subtests**. This handoff also reran `./venv/bin/python -m pytest -q tests/test_litellm_internal_lifecycle_route.py`: **7 passed**. The regression exercises post-body client abort, status 499, canceled upstream read, release/close, and ordinary non-stream completion.
- Bee `006023532` adds a production HTTP visible-periodic-tail test with slot-release assertion, C++ visible-region oracles, and coverage through the grammar/reasoning, loop-guard, and MTP replay focused suites. The existing focused CTest set (`test-mtp-loop-guard-rollback`, `test-reasoning-budget`, `test-server-loop-guard`, `test-dflash-plumbing`) previously passed in both Debug and Release builds.
- Daisy `ba2d383` reports 14 focused tests for failure cancellation, preservation, and retry behavior. The compaction audit also reran the existing Daisy compaction suite: 6 passed.
- The public context-clamp extension has handler-contract coverage and a real-session audit-only integration test. It is installed audit-only, so it records no prompt, payload body, headers, or credentials.

## Reproduce the two lifecycle regressions

These are isolated tests; neither uses the live gateway ports.

```sh
# Gateway cancellation: fake ASGI receiver and fake upstream; no network service.
cd /home/tnunamak/applications/openai-proxy
./venv/bin/python -m pytest -q \
  tests/test_litellm_internal_lifecycle_route.py::InternalCurrentLoadedCancellationTests
```

```sh
# Bee visible-loop finalization: use a separate build directory and test port.
cd /home/tnunamak/applications/beellama
cmake -S . -B ~/.tmp/beellama-visible-loop-0714 \
  -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF -DGGML_CURL=OFF \
  -DLLAMA_BUILD_TESTS=ON -DCMAKE_BUILD_TYPE=Debug
cmake --build ~/.tmp/beellama-visible-loop-0714 -j 4 \
  --target llama-server test-server-loop-guard
PORT=18080 LLAMA_SERVER_BIN_PATH="$HOME/.tmp/beellama-visible-loop-0714/bin/llama-server" \
  ./tools/server/tests/tests.sh \
  unit/test_reasoning_loop_guard.py::test_reasoning_loop_guard_force_close_stops_visible_periodic_continuation
ctest --test-dir ~/.tmp/beellama-visible-loop-0714 --output-on-failure \
  -R '^test-server-loop-guard$'
```

The Bee scenario forces a hidden-thinking close, biases a repeated visible tail, then asserts early stop with `reasoning_loop_guard` and all slots released. A companion test proves a short visible continuation still reaches the normal token limit.

## Remaining limits and next checks

1. **7,500 is calibrated, not a hard bound.** Pi still has no token-accurate retained-tail estimate or postcondition over the rebuilt request. Validate the first post-compaction Vivid Fish request against the 32k operational target.
2. **The exact tokenizer/cancel seam is absent.** A hard request-input ceiling needs authoritative final-payload Qwen tokenization plus a policy to resummarize/truncate long tool-result suffixes. Pi's extension surface also lacks a practical non-patching full replacement-to-compaction-to-retry seam; the clamp extension therefore remains audit-only by default.
3. **No real MTP-model integration proof yet.** Bee's focused tests cover deterministic server behavior, grammar/reasoning, and MTP replay mechanics, but not the affected production MTP model under a real long-running integration workload.
4. **Cache reuse is a separate performance problem.** Retaining an early hybrid-safe checkpoint could avoid future short-prefix resets; do not couple that optimization to loop finalization.

## Rollback

Rollback is per component and should be deliberate:

- Gateway: revert `a22482b` to restore the former current-loaded disconnect behavior. `f3f7e35` changes tests only.
- BeeLlama: revert `006023532` to restore pre-fix visible-tail behavior; this also restores the known post-force-close loop risk.
- Daisy: revert `42a8dab`, `ba2d383`, and `5c831c7` in reverse dependency/order as appropriate. Reverting the 7,500 setting restores a known undercount risk; reverting transactional compaction restores the lossy-fallback risk.

No rollback is required for normal operation. Do not enable context-clamp force recovery without separately accepting its documented false-positive risk for intentional one-token requests.

# Daisy reliability maintainer handoff — 2026-07-14

## Scope and publication state

This is a documentation-only handoff branch derived from `origin/main`. It
does **not** contain the gateway code or tests described below. Those changes
were deployed on an **unpublished operational stack** and are not present in
this branch. Do not treat this document as a code port, cherry-pick, release
artifact, or proof that `origin/main` has the behavior.

The operational gateway process was started at `2026-07-14 05:45:44 CDT`,
after the two gateway commits below. That establishes a restart onto the
operational stack; the separate live probe establishes cancellation through the
public route.

| Area | Operational commit | Result |
| --- | --- | --- |
| Gateway current-loaded cancellation | Vivid Fish `a22482b91634fcc0953afe087976bdf43ee122b5` | Deployed only on the unpublished operational stack. |
| Gateway minimal-reasoning acceptance expectations | Vivid Fish `f3f7e359e2f6ce944b457afe0d4c7369fb847052` | Deployed only on the unpublished operational stack; tests only. |
| Bee post-force-close visible-loop stop | BeeLlama `0060235328b7a19d35b54426184ff6819fcfb6eb` | Production is now clean build `b10111-006023532`; the runtime executable matches the fresh artifact. |
| Safer retained-tail size | Daisy `5c831c7` | `keepRecentTokens` is 7,500 with a 24,576-token response reserve. |
| Fail-closed compaction | Daisy `ba2d383` | Preserve full session on compaction failure and retry safely. |
| Audit-only context-clamp recovery | Daisy `42a8dab`; public extension `2bd18a7` | Records redacted evidence only; force recovery is disabled. |

The earlier cache reset was separate: a changed short prefix had no valid
hybrid/recurrent checkpoint, so Bee discarded unusable cache state. It was not
a 66k prompt re-ingest and was not caused by MTP replay.

## Incident findings

| Symptom | Confirmed cause | Correct owner/fix |
| --- | --- | --- |
| Daisy Escape made the UI idle while Bee kept decoding. | The current-loaded LiteLLM relay used `Request.is_disconnected()` after consuming the body. It is non-blocking and could miss the outer disconnect while an upstream non-stream response was pending. | Gateway `a22482b`: one blocking post-body ASGI receiver watches for `http.disconnect`; cancellation closes/releases upstream and returns 499. |
| Forced hidden-reasoning close was followed by a periodic visible tail. | Production did not feed the visible loop-guard region after force-close. This was visible generation, not hidden-reasoning replay. | Bee `006023532`: guard post-force-close visible tokens and use normal final-response/slot-release when it fires. |
| Pi retained a nominal 20,000-token tail but rebuilt a 51,480-token Qwen request. | Pi 0.80.6's `chars / 4` estimate undercounted tool-heavy context by 2.44x. | Daisy `5c831c7`: use 7,500 recent tokens and keep the 24,576-token reserve. |
| Failed summary could replace history with a lossy fallback. | Compaction preparation and summary failure were not one fail-closed transaction. | Daisy `ba2d383`: cancel and preserve the full session; bounded automatic backoff plus immediate manual retry. |
| Context exhaustion could look like a successful one-token `length` response. | Input consumed the reserved context space. | `2bd18a7` observes redacted metadata in audit-only mode. |

## Gateway porting notes: `a22482b`

This commit cannot be raw-cherry-picked onto `origin/main`: it depends on the
earlier current-loaded lifecycle stack. Port its behavior into the current
routing architecture and prove it with an equivalent ASGI lifecycle test.

Affected operational files and symbols:

- `proxy.py`
  - adds `_await_pre_response_disconnect(request: Request) -> bool`
  - changes `_forward_current_loaded_openai_request(...)`
- `tests/test_litellm_internal_lifecycle_route.py`
  - adds `InternalCurrentLoadedCancellationTests`
  - adds `_BodyThenDisconnect`, `_NonBlockingDisconnectRequest`,
    `_FakeInferenceTask`, `_BlockingInferenceResponse`, and
    `_internal_current_request`

Behavioral pseudodiff (names may differ in the target architecture):

```diff
+ async def await_pre_response_disconnect(request):
+     # The route already parsed the complete request body. It is now the sole
+     # ASGI receive consumer, so block until the outer client disconnects.
+     return (await request.receive())["type"] == "http.disconnect"

  content = await read_llm_response_body(
      client, response,
-     is_downstream_disconnected=request.is_disconnected,
+     is_downstream_disconnected=lambda: await_pre_response_disconnect(request),
  )
  if content is None:
      return Response(status_code=499)
```

The invariant is ownership, not the helper spelling: for a **non-stream**
current-loaded request, after the route has consumed the body, exactly one
blocking receiver may wait for a disconnect while the upstream body read is
pending. On disconnect it must cancel the read and close/release both upstream
response and client. Do not use this watcher while `StreamingResponse` owns
ASGI receive.

The regression constructs a request whose first `receive()` delivers JSON
and whose next call blocks until explicit abort. It asserts the upstream
`aread()` began then was cancelled; HTTP 499 is returned; response and client
are released; one body was delivered; and ordinary non-stream completion still
returns HTTP 200 without a second body delivery.

Recorded operational verification: full gateway suite **2,148 passed, 1
skipped, 387 subtests**; focused
`./venv/bin/python -m pytest -q tests/test_litellm_internal_lifecycle_route.py`
reported **7 passed**.

## Acceptance correction: `f3f7e35`

Affected operational file/test locations:

- `tests/test_proxy_gateway_acceptance.py`
  - `TestGatewayRouterMounting` expectations near lines 473 and 735 in the
    operational parent

This is a test-only correction; it does not change runtime policy. The stale
assertions treated `reasoning_effort: "minimal"` as thought suppression. The
actual ladder is `none=0`, `minimal=2048`, `low=4096`, `medium=6144`,
`high=8192`, `xhigh=-1` (unlimited); only `none` disables thinking.

```diff
- assert forwarded["chat_template_kwargs"]["enable_thinking"] is False
+ assert forwarded["thinking_budget_tokens"] == 2048
```

Keep the adjacent assertion that no `/no_think` content is injected. A Bee
service default may be unlimited, but a request-level finite budget overrides
it; the incident confirmed 4,096 (`low`) and 8,192 (`high`) reached Bee.

## Live public cancellation proof

On 2026-07-14, an external-protocol probe called
`https://ai.vivid.fish/openai/v1/chat/completions` with authenticated
streaming model `current`. It waited for SSE, read exactly one byte, then
closed the response and HTTPS connection. Neither request content nor generated
content was retained.

| Measurement | Observation |
| --- | --- |
| `11:05:09.952Z` | Bee's only slot was idle before the probe. |
| `11:05:12.573Z` (+2620.5 ms) | Live Bee `/slots` showed one active slot with 558 prompt tokens processed. |
| +4277.3 ms | Client received HTTP 200 and `text/event-stream`. |
| `11:05:14.545Z` (+4592.4 ms) | Client read one byte and closed. |
| `11:05:14.668Z` (+123.5 ms) | Bee `/slots` returned to zero active slots; harness completed at `11:05:14.671Z`. |

Bee's correlated journal recorded `cancel task` in the same second. The
journal is second-granularity, so it cannot improve the 123.5 ms measurement,
but it independently distinguishes cancellation from natural completion.
Post-probe public health was HTTP 200 in 14.352 ms; gateway and Bee were active
with zero restarts.

Confidence is **high** that the deployed public gateway-to-current-to-Bee path
propagated this post-response abort promptly. Confidence is **medium** that all
gateway resources were independently observed as stopped: this route had no
per-request gateway lifecycle log/receipt, so that half is inferred from the
closed client and prompt Bee release. The probe used the public hostname and
reverse proxy but originated on the same host. An independent-network probe and
gateway request-ID lifecycle logging would close those gaps.

## Bee and Daisy verification state

Bee `006023532` adds the production HTTP test
`tools/server/tests/unit/test_reasoning_loop_guard.py::test_reasoning_loop_guard_force_close_stops_visible_periodic_continuation`.
It forces hidden-thinking closure and a repeated visible tail, expects
`stop_detail == "reasoning_loop_guard"`, final SSE termination, and all slots
idle. The implementation feeds existing finalization rather than inventing a
second lifecycle:

```text
send_final_response(slot)
metrics.on_prediction(slot)
slot.release()
```

Focused Bee executables passed without starting a server or loading a model:
`test-mtp-loop-guard-rollback` and `test-server-loop-guard`. Earlier
focused CTest coverage also passed in Debug and Release for MTP loop-guard
rollback, reasoning budget, server loop guard, and dflash plumbing.

Production provenance is now closed. Bee was rebuilt cleanly from
`006023532`; its running process reports `b10111-006023532`, and its runtime
executable hash matches the fresh build artifact byte-for-byte. The authorized
`llama-bee.service` restart became active at **06:36:20 CDT**. Post-restart
health, configuration identity, a bounded current-model smoke request, and
post-request slot release all passed. The deployment retains one slot, the
MTP-integrated Qwen 27B configuration (`--spec-type mtp`, no external draft),
and the configured force-close loop guard.

The **only remaining MTP gap** is the controlled real-model
force-close-to-visible-periodic-tail SSE journey. The generic bounded smoke
request reached its token limit while emitting reasoning tokens, so it does
not prove that distinct path. In an isolated/controlled window, run the
focused HTTP test (or equivalent bounded request) and retain only status,
loop-guard fields, stop detail, counters, final SSE completion, and a
post-request slot-idle assertion.

Daisy `ba2d383` reports 14 focused failure-cancellation/preservation/retry
tests; its compaction suite was rerun with **6 passed**. The context-clamp
extension has handler-contract coverage and a real-session audit-only test. It
does not record prompt bodies, payloads, headers, or credentials.

## Rollback and known limits

- Gateway: reverting `a22482b` restores the known current-loaded disconnect
  leak. `f3f7e35` changes acceptance tests only.
- Bee: reverting `006023532` restores the known post-force-close
  visible-loop risk.
- Daisy: revert `42a8dab`, `ba2d383`, and `5c831c7` in reverse dependency
  order as appropriate. Reverting the retained-tail setting restores the known
  undercount risk; reverting transaction safety restores lossy-fallback risk.
- Do not enable context-clamp force recovery without accepting its
  false-positive risk for intentional one-token requests.

The open limits are deliberate: 7,500 is calibrated rather than
token-accurately bounded; Pi has no final-payload tokenizer postcondition; long
tool-result suffixes need an explicit resummarize/truncate policy; and cache
reuse is a separate performance optimization. Do not couple cache work to loop
finalization.

## Port/release gate

Start from a clean, current `origin/main` worktree. Port `a22482b` into the
then-current lifecycle architecture rather than raw-cherry-picking it; adapt
`f3f7e35` only if the effort ladder remains applicable. Run focused lifecycle
and acceptance tests, the required full suite, `git diff --check`, and a
redacted history secret scan. Review the final range before publication. This
branch intentionally performs none of those code-port, test, commit, push, PR,
or restart actions.

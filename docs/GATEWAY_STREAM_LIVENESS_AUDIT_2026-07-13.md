# Gateway streaming liveness audit — 2026-07-13

## Scope and confidence

Read-only audit of tracked gateway code, configuration, design docs, and tests.
No service, socket, process, database, tmux session, or third-party code was
touched. This worktree has no `data/admin.db` (only `data/.gitkeep`) and no
`venv`, so it cannot prove the live registry overlay, environment overrides,
Pi's selected model, or LiteLLM's own runtime behavior.

Confidence is high for gateway code behavior; medium for the exact live route.
The newly supplied 06:22–06:23 observations are incident evidence, not a live
probe performed by this audit.

## Route and `stream: true`

`POST /v1/chat/completions` reads the complete client body, authenticates, and
routes by model (`proxy.py:19327-19409`).

| Request model | Gateway path to Bee | `stream: true` |
|---|---|---|
| Named local model, e.g. `default` | Gateway readiness (`proxy.py:2947-3062`) → LiteLLM (seeded at `registry/services.yml:2-23`) → generated local route (`gateway/litellm_substrate.py:265-328`) → gateway internal `/internal/litellm/local/kobold/...` (`proxy.py:19590-19597`) → Bee `127.0.0.1:5051` (`proxy_config.yml:57-104`, `proxy.py:19462-19472`). | Preserved: the outer body is serialized unchanged (`proxy.py:19147-19173`), and the internal route streams when `body["stream"] is True` (`proxy.py:19495-19513`). |
| `current` / `current-fast` | Direct gateway → active Bee path; LiteLLM is bypassed (`proxy.py:19368-19379`, `3156-3197`, `18922-18965`). | Preserved: no code removes it. The outer direct route recognizes the stream from upstream `text/event-stream` (`proxy.py:18981-19017`). |

The tracked config makes Bee/kobold the default backend and `default` a Bee
model group (`proxy_config.yml:215-249`). Thus `stream: true` is forwarded to
Bee on both possible paths. The exact Daisy/Pi model is not represented in this
checkout, so the incident cannot be assigned conclusively to one path.

The outer routes select their streaming wrapper from the upstream response
content type, not the request field. A `stream: true` request answered without
`text/event-stream` falls into the complete-body reader (`proxy.py:18981-19017`,
`19193-19234`).

## Buffering

* The gateway buffers the incoming JSON request at `request.body()`
  (`proxy.py:19343-19346`), not generated reasoning.
* For SSE, `iter_llm_response_bytes()` yields chunks as it reads them; it has no
  generated-text accumulator (`gateway/backends/llm_execution.py:198-279`). The
  internal Bee route uses the same iterator (`proxy.py:19495-19513`).
* `ToolcallStreamDetector` forwards every chunk unchanged and retains only a
  bounded marker tail from `delta.content`; it does not parse or retain tool
  arguments (`gateway/toolcall_detection.py:179-258`). Streamed tool arguments
  are not normalized by the gateway.
* `_normalize_chat_completion_sse_chunk()` can parse and re-encode an individual
  SSE data line to promote leaked reasoning; it has no cross-chunk buffer
  (`proxy.py:19722-19758`). The stateful think-split transform is opt-in
  (`gateway/transforms.py:727-745`) and is not called by either forwarding loop.
* Only the non-SSE path calls `response.aread()` and buffers the whole response
  (`gateway/backends/llm_execution.py:165-195`). Its non-streaming tool repairs
  run after that read (`proxy.py:19019-19042`, `19236-19289`).

Therefore the gateway stream path does not explain hidden-reasoning or tool-arg
buffering. Those bytes were either withheld upstream (Bee, or LiteLLM on the
named-model path) or buffered by Pi/Daisy. The gateway has no fact to distinguish
those cases before upstream headers or a byte arrives.

## Timeouts and cancellation

The common LLM transport sets `connect=10 s`, `write=60 s`, `pool=None`, and a
caller-selected read timeout (`gateway/backends/llm_execution.py:144-162`). The
tracked `UPSTREAM_READ_TIMEOUT` is `600 s`, env-overridable, and explicitly an
idle/inter-byte timeout—including time to first SSE byte—not a total deadline
(`proxy.py:581-592`, `proxy_config.yml:207-214`).

| Scope | Gateway limit / behavior |
|---|---|
| Gateway → LiteLLM, then gateway internal → Bee (named model) | Each gateway hop uses 10 s connect, 60 s write, 600 s idle read, no pool wait, and no total cap (`proxy.py:19154-19173`, `19462-19472`). LiteLLM's own timeout/cancel behavior is outside this audit. |
| Gateway → Bee (`current`) | Same 10/60/600/no-pool transport (`proxy.py:18956-18965`). |
| Total wall time | None: a byte arriving before each 600 s idle interval can continue indefinitely. |
| Pre-header client abort | No explicit disconnect watcher wraps `client.send(..., stream=True)` (`gateway/backends/llm_execution.py:144-162`). ASGI may cancel the handler, but this adapter does not establish or test that pre-header contract. |
| Post-header client abort | `StreamingResponse` cancellation reaches the iterator, which closes response and client in `finally`; it deliberately does not poll the ASGI receive channel itself (`gateway/backends/llm_execution.py:205-215`, `255-279`, `368-375`). Tests cover this direct relationship (`tests/test_llm_execution_adapter.py:459-492`, `537-579`). |
| Non-streaming abort | A 0.1 s disconnect watcher cancels the whole-body read and closes both resources (`gateway/backends/llm_execution.py:165-195`, `317-375`). |
| Semantic completion | A framed `data: [DONE]` closes upstream even if TCP remains open (`gateway/backends/llm_execution.py:217-220`, `255-314`), tested at `tests/test_llm_execution_adapter.py:297-341`. |

### Incident reconciliation

06:05:09 to at least 06:18:00 is about 773 seconds without an observed backend
byte. If the tracked 600 s setting applied to the Bee hop from acceptance, a
no-byte timeout would be expected near 06:15:09. That mismatch is evidence of a
later actual dispatch, an env override, another deployed revision/path, or an
unobserved intermediary behavior; it is not resolvable from this checkout.

The added evidence is also significant:

* **Proven by code:** a direct post-header downstream cancellation closes the
  immediate upstream connection.
* **Proven boundary:** for a named model, Pi → outer gateway cancellation closes
  the outer gateway's LiteLLM connection. Bee is behind a second LiteLLM →
  internal-gateway connection. The outer gateway sends no explicit cancel signal
  across that sidecar; internal gateway cleanup occurs only if LiteLLM closes its
  downstream stream (`proxy.py:19199-19234`, `19495-19513`).
* **Inference from the supplied 06:22:39 Escape:** Pi immediately reporting
  “Operation aborted” while the old gateway-to-Bee connection (~561,557 request
  bytes) remained ESTABLISHED and Bee stayed 99–100% GPU is consistent with
  cancellation stopping at, or failing to propagate through, the LiteLLM hop.
  It is not proof: the audit did not inspect LiteLLM or Bee runtime state.
* **Inference from the 06:23 low-reasoning turn:** a distinct TCP connection
  without a Bee task-start log is consistent with Bee accepting/queueing a
  connection behind the still-active request. It does not establish whether the
  queue is Bee, LiteLLM, or another runtime layer.
* **Operational proof supplied at 06:26:41:** restarting only openai-proxy
  closed the upstream sockets, and Bee immediately logged `cancel task` for
  active task 2606 plus queued tasks 28141 and 2795 while the model remained
  loaded. Given that Pi had already aborted, this proves the abandoned work
  persisted because the gateway retained the upstream request; it was not a
  Bee/model-lifecycle requirement. It does not by itself identify whether the
  retention occurred in the outer gateway stream, LiteLLM, or the internal
  gateway stream—the named-model topology above is why the liveness record must
  cover each hop.

## Output-time bound

At the stated ideal 21 tokens/s on one 3090:

* 8,192 hidden-reasoning tokens = **390.1 s (6m 30s)**.
* 16,384 total generated tokens = **780.2 s (13m 00s)**.
* If 16,384 visible output is in addition to 8,192 reasoning tokens, 24,576
  tokens = **1,170.3 s (19m 30s)**.

The 13-minute value is the ideal upper bound only if 16,384 is a
backend-enforced total-generation limit including reasoning, decoding begins
immediately, and the rate is sustained. It excludes prompt processing, queueing,
tool turns, retries, and transport overhead. The gateway itself does not enforce
either token budget; it forwards the body (`proxy.py:19147-19173`,
`19435-19468`). Its only bound is 600 seconds of silence, not a total deadline.

## Minimal systemic recommendation

Add one engine-agnostic, gateway-owned active-execution record at the shared LLM
transport boundary—no Bee `/slots` probe and no OpenAI SSE body mutation. Give it
a request id and closed states:

`accepted → dispatched → upstream_headers → first_byte → streaming → terminal`

Terminal reasons: `completed`, `idle_timeout`, `cancelled`, `connect_error`, and
`read_error`. Capture only redacted metadata plus timestamps for acceptance,
dispatch, headers, first/last byte, bytes relayed, and elapsed/silence duration.
Update it solely from facts already seen around `client.send`, headers, and the
shared iterator. This yields the honest state `awaiting_upstream_headers` when
the gateway has no evidence of inference progress.

Expose active records and an append-only event stream through an authenticated
gateway status/SSE surface; persist the terminal summary with receipts. Do not
inject protocol heartbeats into OpenAI SSE. Existing receipts have request IDs
and terminal latency (`gateway/receipts.py:34-43`, `127-154`), but LiteLLM
receipts are written only at terminal completion (`proxy.py:19111-19115`,
`19212-19215`) and the direct-current route creates none.

This one seam covers direct and LiteLLM-mediated Bee paths uniformly, reveals the
currently invisible pre-header wait, and preserves the engine-as-data boundary.
It matches the repository's liveness prior art: named states, append-only events,
pushed progress, and an explicit no-data state rather than inferred health
(`/home/tnunamak/code/dotfiles/ai/research/product-design/run-and-issue-liveness-ux-named-states-earned-recovery-pushed-progress-cli-ui-parity.md`).

## Second systemic finding: process liveness is not composed readiness

The supplied restart sequence is a separate, proven operator-impacting defect:
openai-proxy `/health` returned 200 around 06:26:51, but LiteLLM had not finished
startup/model registration until about 06:27:11, so Daisy retries received LiteLLM
`503 no available server` responses. The 503 wording is runtime evidence; the
readiness split is directly established by code:

* `/health` unconditionally returns `{status: "healthy"}` plus local auth/key
  facts; it neither checks LiteLLM nor the model catalog (`proxy.py:11934-11941`).
* The chat route consults `_service_availability_response()` before forwarding,
  but that function only honors an operator-declared disabled/maintenance/draining
  override; it is not a reachability or registration check
  (`proxy.py:10222-10250`, `19381-19408`).
* `start.sh` starts the LiteLLM watcher in the background and immediately starts
  `proxy.py`; it does not await LiteLLM health or its configured model list
  (`start.sh:76-113`). Docker Compose likewise health-checks only the gateway's
  `/health`; LiteLLM is a separate watcher/process with no readiness dependency
  (`docker-compose.yml:15-20`, `22-45`).
* A true LiteLLM catalog call occurs only on `/v1/models`
  (`proxy.py:16856-16885`), not during gateway readiness. The existing service
  tests prove configuration artifact placement/rewrite, not sidecar startup or
  registration (`tests/test_admin_runtime.py:1110-1159`). The compatibility and
  smoke tests deliberately treat a 200 `/health` as sufficient
  (`tests/compat/python/probe_fresh_instance_journey.py:180-191`,
  `tests/test_smoke_protocol_frontends.py:160-178`).

Keep `/health` as public process liveness, but add an authenticated or
operator-safe `/ready` whose 200 predicate is the composition:

`gateway booted + registry artifact written + LiteLLM reachable + expected local model groups registered`.

Cache the sidecar model-catalog check briefly and publish named non-ready reasons
(`litellm_starting`, `litellm_unreachable`, `model_catalog_stale`) rather than
lying with process health. Point the service-manager/compose readiness gate and
Daisy retry policy at `/ready`, while retaining `/health` for "is this HTTP
process alive?". Add an integration test that starts gateway before a delayed
LiteLLM stub, asserts `/health == 200` but `/ready == 503`, then exposes the
expected model and asserts `/ready == 200`; add the corresponding reconnect and
catalog-regression cases. This is generic substrate readiness, not Bee-specific
probing and does not require `/slots`.

## Verification

Read the canonical code-quality theory, relevant gateway configuration/design
docs, transport/route/transform code, and focused tests. Tests were inspected
but not run because this worktree lacks required `./venv/bin/python`; no
production fallback was attempted. This report is the only repository mutation.

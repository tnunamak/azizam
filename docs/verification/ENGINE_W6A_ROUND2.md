# W6a Round 2 evidence

The executor now isolates unavailable engines and invalid descriptions, checks
public response shapes, and supports bounded list mapping. The paid-provider
restriction applies to new or changed records without blocking reads or repair
of older registries.

Base: `3ce959b10a3918403a3eca2655a832a679c08ce0` (`engine/w6a`).
Implementation checked: `06df2aae` (`engine/w6a-r2`). The report commit changes
only documentation. The independent checker is Claude Sonnet 5.5; its first
three rounds found credential-error and registry-upgrade defects, which were
reproduced before repair. Final recheck and full-suite results are recorded below.

## Changes and scenario evidence

| Item | Result and regression evidence |
|---|---|
| H1 | `inventory.py` calls engines concurrently with a 1 s per-engine timeout and 1.5 s network-phase deadline. Successful lists have a 15 s TTL; the cache holds at most 256 engine configurations. Tests cover unreachable engines, timeout cancellation and draining, concurrent calls, cache reuse, stale issue removal, invalid files and invalid user overrides. Healthy models and chat remain available. File and engine issues reach the existing Status health signal and admin attention projection, with basenames only. |
| H2 | `public_shape.py` checks chat, stream chunks, embeddings and tool-call structure. Tests supply wrong shapes, invalid encoded embeddings, non-finite and oversized numeric values. The first invalid stream event returns typed 502 before headers; a later invalid event emits a typed SSE error, omits DONE and retains a failed receipt. Binary/JSON/stream result kinds must match the intent and request. |
| M1 / D23 | `each` applies nested rules to one selected list. Tests in `test_engine_protocol_mapping.py` cover cumulative iteration/rule budgets, shared depth and deadline, byte limits during growth, lexical scopes and mutable ownership. Grammar, compiler and JSON schema agree. `ollama-native.yml` is data and passes the engine boundary guard without exceptions. Native chat, NDJSON streaming, tags and embeddings run through the public gateway against the reviewer's supplied native response shapes in `test_engine_protocol_native_data.py`. |
| M2 | Round 2 tests assert `chat.completions` receipts, request stream, effective output limit, request shape and accepted/outer-egress stages. Existing extension validation runs before execution: unknown inputs in the known namespace fail; unknown namespaces are stripped as on the legacy path. Embedding dimensions are validated, removed upstream, truncated and normalized with the existing helper. |
| M3 / D24 | New or edited paid/remote executor records fail before persistence. Tests cover config and top-level flags, actual addresses, imports, individual records, full admin saves and runtime refusal of hand-edited data. Old refused records remain readable at boot and visible on Status. Tests with an isolated database prove healthy qualified chat and inventory still work, unrelated writes succeed, disable-only repair succeeds, and re-enable or other edits of the refused record fail. Errors name the record. Loopback is accepted; LAN/container placement remains unknown and is refused. |
| L1 | `test_multiline_sse_held_cr_does_not_end_event_early` splits a multiline event across CR and LF. A mutant that replaced the held CR with LF failed this test. |
| L2 | Public errors use typed plain reasons. Raw internal exceptions are logged. Tests check that server paths and internal text do not appear in JSON or SSE errors; safe bounded operator advice survives. |
| L3 / D19 | Credentials resolve per operation. Tests prove an unused missing control key does not block chat, a missing inference key fails before generation/control, selection and observation use their own classes, and origin/decrypt/database failures remain typed and closed. Healthy inventory survives another record's credential failure. Job operations use the same per-operation resolution. |
| L4 | The fixture description and engine record load from a temporary directory outside the repository. The former substring-only proof was removed. |
| L5 | Public, queued and session image paths call neutral `proxy_image_request`; its legacy fallback alone calls `proxy_comfy_request`. A session runtime delegation test and source sweep pin the entry. |
| L6 | Job tests allow percent and semicolon in engine-returned poll/cancel/fetch path values and check their re-encoding. User path values still reject them. Traversal, slash, control-character and invalid-Unicode defenses remain covered. |
| How-to | `ADD_HTTP_ENGINE_AS_DATA.md` documents the state-directory user files and Docker volume, validation command, native worked example, grammar link, encrypted stored-key walkthrough and local-address/SSRF rules. The CLI validates the bundled native description as three operations. |

Primary regression files are `tests/test_engine_protocol_round2.py`,
`tests/test_engine_protocol_mapping.py`, `tests/test_engine_protocol_native_data.py`,
`tests/test_engine_protocol_w4_followups.py`, `tests/test_service_registry.py` and
`tests/test_session_make_runtime.py`.

## Verification

Fail-before evidence was collected on committed code before each repair. The
initial public regressions had 15 failures and 3 passes. The reused delegated
mapping, credential/path and paid-save tests had 48 failures and 146 passes on
the base. Native mapping had 3 failures. The final legacy-write regression had
6 failures before the write-boundary repair, then passed with the targeted
admin/runtime checks (355 passes and 20 subtests).

The latest full-suite command is:

```sh
scripts/run-hermetic-pytest.sh -q -n 8
```

At `06df2aae`: **11,543 passed, 3 skipped, 1 xfailed and 3,517 subtests passed**
in 97.10 s, with 30 warnings. The earlier full run at `721f8902` also passed
(11,537 tests); the six added regressions cover the final admin-write repair.

The original contract oracle, live-shaped fixture and 13-case legacy wire
fixture are unchanged from `3ce959b1`. The reviewer's wire-capture probe was
reused as `test_engine_protocol_review_wire.py`; it compares the captured request
bytes and public results against a capture from `origin/main` at `5f9a825f`
(five requests plus models). These are fixture-shaped calls, not live captures.

The engine-name ratchet fell from 941 to 939, with `proxy.py` falling from 617
to 615. No guard or allowlist was widened. The boundary test permits engine names
in YAML data. `git diff --check` and native-description CLI validation passed.
No web files changed, so Vitest and Playwright were not run.

The final Sonnet review returned **ACCEPT** for the `06df2aae` write-boundary
repair and found no new blocker in that scope. It checked code and the
fail-before/targeted evidence, without rerunning tests. This was a bounded
repair check, not a fresh audit of the entire executor. The full suite above
was run separately by the implementation agent.

## Mapping cost

The following local measurements include JSON serialization. The checked
hand-written mapper enforces the same iteration, depth, deadline, byte and
ownership guarantees as `each`; it shares the safety helpers. Ratios describe
this machine and workload, not a general latency guarantee.

| List | Executor µs | Checked hand-written µs | Ratio |
|---|---:|---:|---:|
| 1 scalar | 8.147 | 7.315 | 1.11x |
| 32 scalars | 114.572 | 100.301 | 1.14x |
| 256 scalars | 911.033 | 760.800 | 1.20x |
| 32 mutable values | 189.193 | 208.057 | 0.91x |
| 256 mutable values | 1912.261 | 1711.410 | 1.12x |

The result below 1 is timing noise. Against a bare mapper plus JSON, without
those safety checks, scalar cases measured 4.92x, 11.94x and 13.84x. The roughly
3x aim holds for each checked case in the table, rather than an aggregate.
It does not hold against bare mapping. Shared safety costs dominate the checked
comparison.

The existing benchmark measured request mapping plus JSON at 1.37x, parsed SSE
mapping at 3.22x, whole invocation at 2.07x and connection plus invocation at
2.10x. Isolated response mapping was 12.71x a bare mapper. These stub-transport
measurements omit network, inference, admission and public-handler costs.

Reproduce the supported benchmark with:

```sh
./venv/bin/python scripts/bench_engine_protocol.py --each --json
./venv/bin/python scripts/bench_engine_protocol.py --json
```

## Limits and follow-ups

Confidence is high for the hermetic scenarios above. No real engine, live
service, Docker rebuild or browser journey was exercised. Native tests reuse
the reviewer's supplied shapes; they do not establish compatibility with every
engine version. The shipped native description covers text chat and float
embeddings. Chat usage is omitted because the grammar cannot add native token
counters; model creation time is unknown (0). Tool calls, non-text chat and
native base64 embeddings need more description data or a reviewed adapter.
Embedding dimensions retain the legacy base64 skip behavior.

A nonblocking review follow-up remains: generation credentials are resolved once
before selection and again for invocation; the latter lookup is synchronous.
The final reviewer did not separately probe a v1-schema legacy registry or a
refused record containing redacted command markers. A normalization or command
formatting difference could make an unchanged record count as edited and refuse
its save; it cannot enable remote dispatch.
The benchmark's bare-mapping 3x target remains unmet. No performance claim about
real inference latency follows from these microbenchmarks.

Raw local logs, JUnit reports, fail-before outputs, mutation output, benchmark
JSON and reviewer results are under the external `w6a-r2-evidence` directory
beside the worktree. They are evidence artifacts, not runtime dependencies.

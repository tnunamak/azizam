# Gate timing assertions

This audit follows the failed readiness assertion on `ci/gate-speed` at
`a1dfee34`: a valid 10 ms readiness timeout returned in 69 ms under load and
failed the test's 60 ms wall-clock ceiling. The gate must check the deadline
contract without requiring the host scheduler to run the test within 60 ms.

## Scope and search

The audit covered `tests/`, Vitest tests under `web/`, and the Node tests run by
`scripts/ci/pi-unit` and the compatibility step. Searches included
`perf_counter`, `monotonic`, `time.time`, aliased time imports, elapsed/duration
comparisons, `Date.now`, `performance.now`, `hrtime`, timing expectations, and
short sleeps followed by completion assertions. Independent review extended the
search to implicit completion windows, including process tick counts and
background credential commands.

The timing replacements retain the original test cases, production behavior
and configured deadlines. No latency constant was raised. Full validation also
exposed the pointer-focus defect described below. New
30-second watchdogs only terminate broken event protocols; they do not decide
whether a valid operation is fast enough. Tests still fail on wrong deadlines,
wrong poll intervals, missing cancellation, premature work, or missing progress.

## Replacements

| Tests | Previous host-clock dependency | Deterministic contract |
|---|---|---|
| `test_engine_contract_runtime.py`: readiness | 10 ms probe deadline must return within 60 ms | Capture the exact timeout argument on a frozen monotonic clock; expire it and assert the false readiness result. |
| Same file: residency | Control and observation must return within 60 ms | Advance an injected clock during the initial observation; assert the control operation receives only the remaining stall budget. |
| Same file: idle responsiveness and ownership | Sleep 50/160 ms, then require a responsive loop or a stopped process | Gate the declared idle sleep, release it with an expired last-use timestamp, and await the idle task. A blocked termination worker must run off the loop thread while the loop continues. Active requests and engine-owned idle policy remain covered. |
| Same file: process logging and restart | Assume log pumping or process exit after 50 ms | Use readiness ordering and await the owned supervisor's exit. |
| `test_engine_protocol_round2.py`: partial inventory | Healthy reply and hung-engine expiry must complete within 1 s | Hold the hung invocation, observe the healthy reply, then expire the captured per-engine deadline. Check both exact engine budgets, the listing budget, the healthy model and the failure issue. |
| Same file: total inventory | A real 30 ms total timer can fire before the mocked invocation starts | Wait for invocation entry before firing the captured total deadline. Assert cancellation cleanup and the total-time-limit issue. |
| `test_engine_protocol_round3.py`: slow description reads | Return before listing budget plus 500 ms while a worker sleeps 3 s | Hold a real loader thread on an event; fire the captured description-read deadline only after it starts. Assert the listing returns before that worker is released and preserves the last good description when available. |
| Same file: forty hung engines | Aggregate listing must finish within a host-clock bound | Start all forty invokes, fire one captured listing deadline, and assert cancellation and issues for all forty. |
| `test_engine_protocol_executor.py`: body, fetch and job polling | Real 300 ms–1 s deadlines, 490 ms spacing, and 1.5–2 s elapsed ceilings | Run fake transports on a virtual asyncio loop. Assert exact poll timestamps, exact elapsed virtual deadline, cancelled/closed partial bodies, and shared asset-fetch budget. A poll-count cap fails a missing-yield regression. |
| `test_swap_drain.py`: timeout | 100 ms drain must return in 90 ms–2 s | Inject the virtual loop clock and assert deferral at exactly 100 ms with the original lease still counted. |
| Same file: old lease diagnostic | Formatting `10800s` silently requires less than one extra host second | Advance the virtual lease clock by three hours and run the original 50 ms drain deadline on that clock before checking the exact diagnostic. |
| `test_extension_packages.py`: transaction | A competing save must remain incomplete for 500 ms | Attempt a second SQLite write transaction with `timeout=0` while removal holds its transaction; require a locked result, then a successful plan save after commit. |
| `test_engine_process_review.py`: cold start and adopted stdout | Cancel after 150 ms; require four child ticks within 400 ms | Cancel only after the readiness probe enters. Await observed child-marker progress under the existing coarse process hang guard. |
| Pi `provider.test.ts` | Five operations must return within 900 ms | Capture the configured overall/phase AbortSignal budgets, observe restore/credential/fetch entry and phase-budget creation, then abort that phase and assert the catalog, authorization, and safe error behavior. |
| Pi `store-lock.test.ts` | Reads/writes must finish within 2–3.2 s | Observe stale-lock recovery, or expire captured lock budgets and assert fallback reads, skipped writes, retained live locks, and one diagnostic. |
| Pi `runtime-isolation.test.ts`, `pi-rpc.test.ts` | Background work is assumed finished after 300–1000 ms | Await native-registration refresh promises in process. A test-only Pi extension records when the actual startup-refresh promise settles; await that unique marker before checking isolation and persistence. Read command counts after refresh completion and child exit. |
| Shell deferred checks, owner Status, Add engine polish | Observe delayed or pending UI after 20/150/1600 ms | Advance fake timers inside React `act`, hold response promises explicitly, and retain the same pending-state and no-fake-progress assertions. |

The virtual asyncio loop is test-only. It advances to the next scheduled timer
when no ready callbacks remain, using the real asyncio timeout/cancellation
machinery. It is restricted to in-process fake transports and swap-drain tests;
real sockets, subprocesses and worker threads use explicit events instead. Its
small adapter depends on CPython's event-loop internals, matching the pinned
Python used by the hermetic runner.

## Reviewed exclusions

- `test_request_budget.py` measures latency for diagnostic text, but its only
  budget assertion counts SQLite opens. It is already independent of host time.
- `test_image_polling.py` computes elapsed time from recorded fake sleeps.
  Media durations, receipt timestamps, token expiry and fake-timer comparisons
  are data or virtual-time checks, not host execution budgets.
- Ten-second engine inference/dispatch bounds, process startup/cleanup polling,
  and other coarse hang guards are retained. Their purpose is to fail a stalled
  fixture, not to enforce a sub-second performance policy.
- The first-run Playwright journey retains its explicit ten-minute release bar.
  Ordinary Testing Library/Playwright eventual-assertion timeouts are not
  sub-second latency assertions.
- Short thread joins used only to create overlap were reviewed separately from
  elapsed performance assertions. They are not evidence of request latency.

No genuine sub-second latency policy needed relocation to `scripts/perf`.
The replaced checks concern configured deadlines, cancellation, ordering and
progress. Real endpoint performance remains in the existing performance harness.

## Focus failure during full validation

The second full gate exposed an independent pointer-focus race in the People
invitation dialog. Radix prevents pointer-down from focusing a menu trigger.
The page remembered only focused buttons, so dialog focus restoration depended
on the dropdown's later autofocus beating the dialog's focus trap.

The page now captures the pointer opener before opening the menu. The existing
recipient-confirmation test suppresses the intermediate trigger-focus attempt,
flushes the menu callbacks with fake timers, then closes the dialog and checks
focus. Before the fix it deterministically focused the Invites tab; with the
fix it returns to the invitation's Actions button. The browser test retains its
original pointer interaction and focus assertions.

The next full gate also exposed fixture startup costs: smoke checks exceeded a
ten-second process guard, and the provider watcher missed its initial marker.
The smoke fixture now uses a shell `curl` stub instead of starting Python for
each fixed response. The watcher fixture uses a shell sidecar stand-in and
returns its explicitly configured DB path directly; secret fingerprinting and
the sidecar's real environment injection still run through production code.
The existing guards, digest checks and test cases are retained.

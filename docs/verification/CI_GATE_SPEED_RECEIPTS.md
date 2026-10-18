# Gate speed receipts

## Round 1 measurements, 2026-10-18

This section records the implementation at the time. Round 2 below supersedes
its environment-key policy and venv snapshots.

The baseline is `5f9a825f`. Two cold and two warm installed-cache runs on
`3ba8b6ca` kept every gate step. All four passed. Cold means empty installed
snapshot entries; npm/uv download caches, Python toolchains and Docker layers
were retained. Each run used the heavy wrapper, started below load 48, held the
host gate lock and used port 18440 without signoff.

| Run | Elapsed s | Critical path s | Step wall sum s | Receipt CPU s | Peak RSS MB |
|---|---:|---:|---:|---:|---:|
| baseline | 380 | 377.91 | 822.81 | 1141.63 | 1340.8 |
| cold-1 | 290 | 288.18 | 653.72 | 1159.16 | 1341.3 |
| warm-1 | 282 | 280.05 | 666.00 | 1152.10 | 1336.7 |
| cold-2 | 297 | 294.34 | 660.67 | 1161.35 | 1340.2 |
| warm-2 | 292 | 289.42 | 688.58 | 1150.90 | 1318.7 |

The cold median was 293.5 s and the warm median was 287 s, against the single
380 s baseline. The median over all four runs was 291 s. Host load and order
seeds varied, so these are observed timings, not an isolated causal estimate.
CPU is the sum of step receipts. Peak RSS is the largest single process peak,
not concurrent host memory. Docker daemon work is outside these CPU/RSS meters.

### Where the time went

- The npm download cache already worked: an inspected cold-install log had
  739 package-cache hits, zero misses and zero HTTP fetches, yet install took
  15 s. Installed Node snapshots reduced the measured step from 20.17/22.61 s
  cold to 5.98/7.83 s warm. Dependency hooks require a Git-tree key, so this
  reuse applies to repeated trees rather than arbitrary source changes.
- The main browser step fell from 258.21 s to 198.42-203.27 s. The existing
  two-server global-state split and early serial People project remove two
  serial scheduling tails. An intermediate run with only the global-state
  split still took 257.81 s for that step because People started late.
- Browser venv and Chromium setup now run beside build/unit work. Their former
  0.25 + 0.75 s dependency chain is absent from the critical path. Installed
  venv copies took 2.98-4.02 s, slower than the previous uv install; the venv
  cache remains as requested, off the critical path, with bounded retention.
- Unit-test wall time also varied from the baseline's 93.45 s to 64.75-81.99 s.
  Its command and worker count did not change, so this difference is not
  credited to the cache or browser scheduling. Journey-clock, cases and policy
  still join all required jobs; their short sequential tail is unchanged.

The critical path in each of the four runs was:
`web-npm-ci > web-unit > e2e-main > e2e-tree-unchanged > journey-clock > cases > policy`.

### Coverage and isolation proof

`compare.py` in the local evidence directory compares full logical case/outcome
multisets, including multiplicity. It normalizes only the two expected project
changes (`chromium-global-state-2` and `chromium-people`). Every baseline case and
outcome remains, with four added pytest checks for the cache and target behavior.
All four runs have 11313 pytest passes, 3 skips and 1 expected failure; 2667 vitest
passes; 460 Playwright passes; 87 Node passes; and 3517 passing subtests. All 20
step receipts are successful, including Compose, native, conformance and policy.

A seeded run exposed a pre-existing fixture collision: another test recorded the
literal `req_unknown` before the missing-parent assertion used it. A two-test
reproduction failed before the fix. The fixture now generates a fresh request ID
and retains its assertion; the contaminating test followed by all identity tests
passes (7 tests). The failed run remains in the evidence directory.

Real relocated uvicorn launchers imported packages from their own private copies.
Changing copy A left copy B and the snapshot unchanged. Independent Playwright
listing compared the baseline and revised main partition: both contain 442 cases,
with all 83 untagged People cases moved exactly once and the tagged case still in
a global-state project. The separate isolation-proof listing retains its original
30 cases and excludes the 83 cases that the new project would otherwise add.

### Follow-up verification

Independent review requested bounded snapshot retention. The final helper keeps
current plus one recent peer per kind, skips active keys under nonblocking locks,
and validates snapshot contents before copying. The focused cache, gate and
identity tests passed (49 tests), including locked-key protection and later
eviction. The final proof config also passes TypeScript checking.

The retention/proof and cross-invocation-key follow-up on `877fb51b` was checked
separately from the four-run timing set above:

| Run | Elapsed s | Critical path s | Step wall sum s | Receipt CPU s | Peak RSS MB |
|---|---:|---:|---:|---:|---:|
| final-verification | 283 | 281.40 | 635.30 | 1145.90 | 1337.0 |
| final-warm | 350 | 347.98 | 794.36 | 1228.08 | 1119.6 |

These follow-ups run in separate tool invocations and require actual cache hits
on the warm run. A cross-invocation diagnostic exposed `CODEX_TOOL_CALL_ID` in the
key; the helper now removes this execution label from its own environment before
both hashing and installer execution. A regression supplies two different call IDs
and requires warm hits and exact case parity. All other installer environment
inputs remain in the key. Both follow-ups are checked against the same complete
case/outcome multiset as the four-run set. Snapshots are bounded to two per kind
at rest, with active keys protected. Free disk space was not checked; a full disk fails
the install and removes the partial entry.

An earlier diagnostic at `ca905621` took 347 s with a Compose web-image rebuild
(build 67.5 s versus about 2 s in the controlled four-run set), and 321 s when
that image was cached but the tracing label still forced installed-cache misses.
Those receipts remain separate from the controlled cache-hit comparisons. Exact
wall time remains sensitive to Docker build state and shared-host contention.
The final cross-invocation Node cache hit took
43.44 s (10.46 CPU-s) after another gate had run, compared with 5.98-7.83 s in
the controlled hot-cache trials. Hashing and full copies on ext4 can therefore
cost more than a fresh npm install under storage/scheduling contention; cache
reuse is verified, but it does not guarantee a wall-time saving in every run.

Full receipts, raw reports, logs, `comparison.json`, comparison scripts, real
copy-isolation checks and independent reviews are retained locally under
`~/Documents/azizam/gate-speed-evidence/`. The failed fixture trial and the
TypeScript build failure found during the proof-config change are retained
separately; neither is counted as a successful run. No retries, skips or timeout
relaxations were added to make these measurements pass.

## Round 2, 2026-10-18

Merged main r67 (`586a3417`). The Python ceiling is main's measured 11677 plus
four cache/target-budget cases, 11681; the full gate measurements below verify it.
The four existing regression cases now also check unrelated/session variables,
`npm_config_registry`, existing destinations, root mode and foreign ownership.

One sequential venv comparison on the same lockfile, retaining uv downloads:
population 4.294 s, validated snapshot hit 1.683 s, fresh `uv venv` plus
`uv pip sync --require-hashes` 0.125 s. The snapshot cache was removed because
its restore was slower. Node snapshots remain. No disk-space preflight is claimed.
Raw comparison logs: `~/Documents/azizam/gate-speed-evidence/r2/venv-benchmark/`.

The exploratory gate on `3a0ffe6d` started at load 11.19 with no other gate.
It took 323 s with a 321.04 s critical path. All 2676 Vitest cases passed in
79.78 s; the slowest case was 4184.263 ms (`try-image-run.spec.tsx`, connection
drops while polling). Reading that case shows an existing 15 s timeout, not the
default 5 s timeout. Every other case was at most 2776.326 ms, so no case was
within one second of its applicable limit. No timeout was changed.

A serialized diagnostic on `626a4805` passed all 20 steps in 300 s with a
297.90 s critical path. Vitest took 50.89 s; the 15 s polling case took
4075.643 ms and every other case was at most 2862.347 ms. The diagnostic was
based on initially comparing all cases against the default 5 s limit. Because
the four-second case explicitly allows 15 s (two real 2 s polling intervals),
that comparison did not establish a timeout risk. Parallel scheduling was
restored before final verification. Its full receipts and logs remain under
`~/Documents/azizam/gate-speed-evidence/r2/serialized-diagnostic/` and the
adjacent `serialized-diagnostic.log`. Final verification runs below use the
parallel schedule.

That exploratory gate failed one newly merged main test:
`test_engine_protocol_round2.py::test_total_inventory_deadline_reports_cancelled_engines`.
The 30 ms total inventory deadline expired before the mocked invocation began,
so the test's `closed` list was empty. Counts: 11676 passed, 1 failed, 3 skipped,
1 xfailed (11681 items), 3524 passing subtests. Raw receipts are retained in
`~/Documents/azizam/gate-speed-evidence/r2/exploratory/`. This failed run is not
counted as a passing final verification.

### Final parallel verification

Both runs tested `abce87fe7d495f6503b373cedd8db63fd7454425`, with the command
`HEAVY_PRIORITY=1 ~/Documents/azizam/tools/heavy scripts/ci-local --force --target main --port 18100`.
No signoff was requested or posted. Each admission checked load below 48 and
that no other `ci-local` was running. Admission loads were 27.02 and 27.18;
load later rose into the 40s and above 48 during the second run.

| Run | Verdict | Elapsed s | Critical path s | Step wall sum s | Receipt CPU s | Largest step RSS MB |
|---|---|---:|---:|---:|---:|---:|
| verified-1 | PASS | 279 | 276.30 | 598.34 | 1104.63 | 1348.1 |
| verified-2 | FAIL | 299 | 297.00 | 655.09 | 1108.70 | 1138.7 |

Both critical paths were:
`web-npm-ci > web-unit > e2e-main > e2e-tree-unchanged > journey-clock > cases > policy`.
CPU/RSS retain the measurement limits described above; Docker work and concurrent
host memory are not included. These two runs on r67 do not create another paired
baseline. They cannot isolate the cache's contribution from host contention.

| Run | Node cache | Node setup s | Browser venv s | Vitest step s | Polling case ms (15 s limit) | Source guard ms (15 s limit) | Maximum remaining case ms |
|---|---|---:|---:|---:|---:|---:|---:|
| verified-1 | miss | 18.17 | 0.29 | 64.67 | 4175.739 | 2656.621 | 2249.232 |
| verified-2 | hit | 16.93 | 1.12 | 79.16 | 4225.700 | 4189.919 | 2290.310 |

The two cases above already specify 15000 ms in `try-image-run.spec.tsx:121`
and `model-voice-discovery-guard.spec.ts:196`; neither timeout was changed.
Every remaining case took at most 2290.310 ms across both runs, below the
4000 ms threshold for serializing work to protect the default 5 s limit.
Parallel scheduling is retained. Both runs passed all 2676 Vitest cases.
The Node key was identical across the miss and hit. No venv snapshot is used.

The logical case inventory is identical across both runs: 11681 pytest items,
2676 Vitest cases, 460 Playwright cases and 87 Node cases. The first run passed
all 20 steps: 11677 pytest passes, 3 allowed skips, 1 allowed expected failure,
and 3524 passing subtests. The second passed 19 steps; only `python-hermetic`
failed. All browser cases, native and Compose journeys, conformance, policy and
cache regression cases passed. The exact case/outcome comparison found one
changed outcome:
`test_engine_contract_runtime.py::test_process_readiness_probe_respects_total_deadline`.
It completed in 69.076 ms against its unchanged 60 ms wall-clock assertion.
This test and the earlier inventory-deadline failure are unchanged from main
r67. They remain observed timing failures; a passing run does not fix them.
No assertion, skip, retry policy or timeout was weakened. These results do not
establish a consistently green release gate under contention.

The focused cache/gate suite passed 43 tests. The serialized scheduling diagnostic
and inventory-deadline check passed 44 focused tests. Regression checks cover an
unrelated variable and changing session labels leaving a warm key unchanged,
`npm_config_registry` changing the key, existing destinations retaining their
contents on refusal, mode 0700, a custom root whose uid does not match the caller,
failed-install cleanup, damaged snapshots and eviction locks. The foreign-owner
regression injects a mismatching caller uid; it does not require chown privileges.

Raw final logs and full reports are saved under
`~/Documents/azizam/gate-speed-evidence/r2/verified-{1,2}/`, with adjacent
`verified-{1,2}.log`, `comparison-final.json` and `compare-final.py`.
The final receipt commit changes documentation only after the tested SHA.

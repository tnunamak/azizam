# Test strategy

Status: design v3.1, final for the overhaul, 2026-10-12. Nothing here is built unless a line says "exists".
Base: `main` at `a97c0ce7` (release 20.1, includes `chore/fast-gate`: pytest-xdist, 10 browser lanes, `scripts/test-affected`). v3.1 rebased on `main` at `8774d3c3` (S1 landed); source line numbers still refer to `a97c0ce7`.
Inputs: research in `~/code/divan/test-suite-excellence/` and `~/code/divan/testing/`; the audit in `~/Documents/azizam/test-overhaul/audit/` (`consolidated.md`, `sensitive-audit.md`, `measurements.md`, `mutation-raw/`, the `dot/azizam-tso-*` proposals); the v1 review `audit/dot/judge-design.md` and the v3 review `audit/dot/judge-v3.md` (both resolved in section 9). `audit/mutation.md` does not exist; scores below come from `mutation-raw/`.
v3 inputs: `testing/gateway-tests-should-default-to-fake-provider-and-replay-lanes-with-live-canaries-and-quality-evals-separate-opt-in-and-budgeted.md` and `testing/browser-e2e-suites-stay-non-flaky-through-isolated-data-deterministic-fixtures-web-first-assertions-and-first-attempt-flake-measurement.md`. Both are unverified worker reports; their numbers are context, not targets. Section 11 gives the disposition of every recommendation.

Owner rules, applied everywhere:
- No automatic retries. A first failure stays a failure.
- No coverage gate. Count and time are budgets.
- Every behavior change carries a test that fails before the change and passes after it. A change with no behavior change says why no test applies.
- Every deleted test states its reason and names the test that still catches its defect.
- Browser lanes never install `node_modules`. `ci-local` installs once (`npm ci`) and hard-links it into each lane (exists, `ci-local` 202-214). Keep that.
- Engines are data in tests too (AGENTS.md). A fake models a wire protocol the registry accepts, never a named engine. An engine quirk under test is a scenario row plus the transform config row an operator would write (4.6).

## 0. Findings and decisions

| Finding | Decision |
|---|---|
| Strong suites gate the integrated candidate and run slow tiers on a schedule; affected-only selection is safe only with a full backstop (Shopify). | Three tiers. The PR gate is the **full** gate on the exact merge candidate. Selection serves the local loop only. |
| Kubernetes banned retries and moved flakes to owners. | Zero retries. Quarantine only through the contract in 3.3, built when first needed. |
| Python tier: 287 s wall (229-325) on 12 workers, CPU/wall 1.3. With the whole state root on tmpfs: 40 s, same 7,884 passes, test-seconds 2,430 → 244 (one run). | The tier is fsync-bound. S0 moves pytest's temp directory to tmpfs, measured by its own A/B (4.4). |
| Browser main config: 363 s, ~2,700 CPU-s per pass, 7.5× the Python CPU. Slowest lane 279-352 s. | After S0 the browser lanes set the gate wall. S14 is the time slice. |
| Flakes: Python 0 in 6 runs (order never shuffled); Playwright 0 in 3 (one fixed plan); vitest `job-notices.spec.tsx` 1 in 5 at load 35-40. | F0 fix landed (`a97c0ce7`). Shuffled order arrives in T0, so the zero is not yet evidence. |
| 16 files run in no gate on this branch: 14 `*.e2e.ts` (≈57 tests), `app-shell.playwright.spec.ts`, `services-route.spec.mjs`. 10 area Playwright configs are wired to nothing. | A discovery guard fails on any test file no tier runs. Known orphans carry an expiry. |
| Mutation baselines for `category_policy`, `category_enforcement` and `policy` had 2 failing tests (`test_route_policy_inventory`). | Those scores are not "before" evidence. T0 re-establishes green baselines. |
| `sensitive-audit` (measurements 5): 3 auth files hide behavior behind mocks (`test_litellm_delegated_auth`, `test_litellm_key_endpoints`, `test_protocol_forward_auth`). | Rewritten over loopback ASGI fakes in S2. |
| **v3.** The hermetic Python tier denies egress in two layers. The boundary is the network namespace (`bwrap --unshare-net`, `run-hermetic-pytest.sh` 53). `tests/_hermetic.py` is a Python connection diagnostic inside it: it hooks `connect`/`connect_ex`, and a port remembered from a wildcard bind is allowed to any host. Its own docstring gives native-child protection to Bubblewrap. No browser guard is visible in `playwright.config.ts` or `serve.py`. | Python: keep both layers; S0 adds the guard's negative tests and the loopback-address check (7). The runner's hook installation stays UNVERIFIED until then. Browser: BI1 adds a guard (3.4.9). |
| **v3.** Upstream fakes are scattered: 59 test files start their own HTTP app or server, 14 use `httpx.MockTransport`; the browser fake is `scripts/ui/fake_engine.py` (166 lines); `tests/compat/harness/fake_backends/` holds a third set. No fake has fault scenarios (split SSE, disconnect, 429). | FK builds one scenario-driven fake upstream per registry protocol. Existing fakes migrate inside their owning slices; the count is a ratchet. |
| **v3.** No test runs against a real upstream on a schedule, and no recorded real-upstream transcript exists. Fakes are written from our idea of the protocol. | RP adds reviewed replay transcripts served by FK. LV adds a budgeted live canary lane outside the gate. |
| **v3.** Browser lanes share one owner and one seed per lane (`global-setup.ts`), with `workers: 1`. Tests in a lane mutate shared server state in sequence. T0's seeded file-to-lane plan will change each test's neighbors. | BI2 adds a per-invocation isolation fixture (3.4.7), as a fixture PR then named batches. T0 keeps the legacy browser file-to-lane grouping until the last batch lands. No lane or worker increase before then. |
| **v3.** Browser hygiene (grep at `a97c0ce7`, T0 recounts with the lint): `waitForTimeout` 19 in 10 files; `force: true` 4 in 3; `networkidle` 12 in 11; one-shot `expect(await …)` 11 in 7; bare `.isVisible()` 6 in 6. `page.route(` 329 in 44 files; only 4 of those files read `web/e2e/fixtures/`, so most intercepted bodies are inline and unchecked against the server. `toHaveScreenshot` 0. | Ratchets from T0; zero by the end of S14. Intercepted JSON gets a schema check in G2. |
| **v3.** `scripts/ui/serve.py` writes each lane's gateway and fake-engine log into a `TemporaryDirectory` that is deleted at exit. A failed browser case keeps its trace but loses the server side. The log can hold key material (`serve.py` comment). | Keep the lane logs of failed runs, private (0600), in the receipt directory. Never paste them. |

## 1. Tiers

### 1.1 Local loop

```bash
scripts/test-affected --list          # selection, also as JSON (2)
scripts/test-affected -- -n 4         # affected Python + vitest --changed
scripts/test-affected --e2e -- -n 4   # + Playwright --only-changed (build web/ first)
```

Lanes use 4 workers. Many lanes share one 24-core host. A local pass is never merge evidence.

### 1.2 PR gate on the merge candidate

```bash
scripts/ci-local --force --signoff --target main   # at the merge candidate commit
```

- Merge candidate: the target tip with the PR merged, the exact commit that becomes the new tip. It runs everything `ci-local` runs today plus T0's `policy` step.
- Exclusive host lock (`flock ~/.tmp/azizam-ci.lock`): two gates share 24 cores and ports 18000-18998. Compose still finishes before browser lanes start (`ci-local` 147-152). Scheduled runs (1.3, 1.4) take the same lock, one task at a time, never for a whole run.
- **Gate priority (v3.1).** A waiting gate writes `~/.tmp/azizam-ci.gate-waiting`. A scheduled run checks it before each task; if it exists, the run releases the lock and waits until the file is gone. So a gate waits at most one scheduled task: ≤ 20 min, the largest task allocation (1.3; weekly tasks are split into chunks of ≤ 20 min). A gate that waits longer than 30 min exits nonzero with `LOCK_TIMEOUT`, never green. The receipt records lock wait apart from execution time, and the total the user waited.
- The gate uses only deterministic upstreams: FK scenarios and RP transcripts. No credentials, no paid call, no live engine. A test that needs a real upstream belongs to 1.4.
- **Signoff receipt (T0).** `--signoff` is accepted only when all of these hold, else it exits nonzero:
  1. This invocation ran every required step fresh. The tree cache (`ci-local` 50-54) stays for development runs and never yields a signoff.
  2. The receipt records SHA, target branch, target tip, toolchain fingerprint (Python, Node, Chromium, ffmpeg), seed, the required step set and the `policy` result. A partial or interrupted run writes no receipt.
  3. `HEAD^1` equals the recorded target tip, and `git ls-remote` still returns that tip just before `gh signoff create`.
  4. No open `nightly-red` issue exists, unless the PR closes it. `live-red` (1.4) does not block.
- Limit: GitHub does not verify a signoff and the repository has no branch protection (`docs/ops/CI.md`), so "merge only a SHA with a `signoff` status" is a rule, not a lock. Branch protection requiring `signoff` would make it a lock (owner decision; availability UNVERIFIED).

### 1.3 Scheduled tier on the release tip

`scripts/test-scheduled nightly|weekly` (T0). A scheduled pass is never gate evidence.

**Owner decision (reversible, default recorded 2026-10-12):** a systemd user timer starts it on the live machine at 04:00 local, `nice 19` and idle I/O class, at most 8 Python workers and 4 browser lanes, ports 18xxx only. The night is skipped if load average > 24 or a media job is in flight (read-only status query; endpoint UNVERIFIED). A skip is recorded as `SKIPPED`, never as a pass.

Nightly, 60 min, with provisional per-task allocations: web build 6; full Python, 3 seeds, 9; vitest, 5 seeds, 4; browser main config, one seed, 15; full browser matrix + sweep config + Firefox media project (missing Firefox fails) 20; SDK probes on a disposable 18xxx instance over FK upstreams (`tests/compat/node`, `probe_openai.py`) 6. A task that runs out of time writes `INCOMPLETE`, never green. All nightly tasks are deterministic. Real upstreams run only in 1.4.

After the last BI2 batch lands (7), the nightly browser main config uses a different lane-plan seed from the gate. A case that fails only with new neighbors is an isolation defect (3.4). Before then the second seed would only find specs that BI2 has not migrated yet.

Weekly, 3 h, in tasks of ≤ 20 min each (gate priority, 1.2): mutation of the core modules (4.5), selector recall replay (2), borrowed conformance against latest upstream releases (4.2), Schemathesis over the local OpenAPI with FK upstreams (4.2.6, from G2), the RP transcript age report (4.7). Hard walls exclude lock wait and include cleanup. A run that yields to a gate and then runs out of its window records `INCOMPLETE`.

**Weekly report.** One file in the receipt directory; the run also posts its summary as a comment on one pinned `test-health` issue. The owner reads it. It lists first-attempt failures and confirmed flakes (3.1), live totals and staleness (1.4, 4.7), and receipt-directory size.

**Performance task (`perf`).** After the sweep, the nightly runs `scripts/perf/run.py`: a gateway on a read-only copy of the live databases and media, every GET route timed (p95, SQLite statements, connections, processes per request) and every page loaded in a browser (time until `<main>` stops changing, requests, bytes, long tasks), judged by `scripts/perf/budgets.toml`. Over budget is `FAIL` and opens or updates the issue labelled `perf-red`; it does not block signoff, and the harness never writes the live data. The counts (statements and processes per route at two data sizes) are also in the gate: `tests/test_route_work_budget.py`, `tests/test_work_budget.py`. Times only mean something on a quiet host, which is why the night skips when the load is over its limit. Diagnose a slow route with `scripts/perf/attribute.py` (which code issued each statement or process) and `scripts/perf/profile.py` (where the time goes).

**Red night.** Any failure opens (or updates) a GitHub issue labelled `nightly-red` with seed, case IDs and receipt path. While it is open, signoff refuses (1.2). Diagnose the same day. If no bounded fix exists, revert the change that introduced it. Rerunning until green is not recovery.

Receipts: `~/.tmp/azizam-ci-receipts/` keeps gate, scheduled and live receipts for 90 days and at most 20 GiB; past that, the oldest run directory is deleted first. A PR that cites evidence copies the needed lines into its description, so the evidence outlives the file.

### 1.4 Live lanes (v3, slices LV1-LV3): opt-in, budgeted, outside the gate

A live result says whether today's real upstream still speaks the contract. It says nothing about a candidate commit, so it never gates and never blocks signoff.

**Scope.** Real upstreams only: the operator's local engines and paid remote providers. The lane runs a disposable 18xxx gateway with the canary services registered through the admin API, as an operator would. It never touches ports 5000/15000 or the live `admin.db`.

**Engines as data.** Committed code is generic: one canary case per protocol × capability, reusing the S6/S7 conformance cases with the upstream switched from FK to the real service. Lane code branches on protocol, capability and step type only. Which services, models, credentials, prices and limits to probe is operator data in a gitignored `canaries.toml` (path in `.env.local`). RP1 ships its schema, its validator and `tests/live/canaries.example.toml`, because the recorder reads it first (4.7). Capability scope grows by milestone: LV1 and LV2 cover chat only; LV3 adds media after S5a/S5b have built the media oracles (4.8).

**Counting proxy.** The lane registers each service with a base URL that points at a lane-owned loopback reverse proxy, which forwards to the real upstream. The proxy is the outbound boundary: it counts every request the gateway makes (health checks, retries, fallback, polls, downloads), refuses any request past a case's limits, and records the status the upstream actually returned. No gateway code changes for this. An artifact download from a host other than the service's base URL does not pass the proxy; such a case declares that host in `canaries.toml`, the proxy also fronts it, and an undeclared host fails the case.

**Schedule.** `scripts/test-scheduled nightly-live` starts when the nightly finishes or is skipped (a systemd `OnSuccess=`/`OnFailure=` chain, not a fixed clock time), with the same `nice`, I/O class, skip rules and per-task lock (1.2). If it cannot start by 06:00 it records `SKIPPED` with the reason. `weekly-live` follows the weekly the same way. Separate receipts.

**Budgets (provisional until two weeks of receipts exist):**

| Control | Nightly-live | Weekly-live |
|---|---|---|
| Hard wall (excludes lock wait, includes cleanup) | 10 min | 20 min |
| Cases | the core set marked `nightly = true` | core set + a rotation through the rest of `canaries.toml` |
| Concurrency | 1 request | 1 request |
| Requests per case | finite limits per protocol × capability in `canaries.toml`, enforced by the proxy on one shared counter per case (below) | same |
| Output bounds | chat ≤ 32 tokens; image ≤ 256 px side, 1 image; speech/music/SFX ≤ 3 s | + video ≤ 2 s at the lowest listed resolution |
| Paid spend | one monthly ceiling shared by all live runs (below); default 0, so paid cases are `SKIPPED` | same ceiling |

**Request limits (v3.1).** Each protocol × capability row in `canaries.toml` sets `max_requests` (the shared total), and within it `max_submits`, `max_polls`, `poll_interval_s`, `max_retries`, `max_downloads`, `max_cancels` and `wall_s`. Chat can keep its two-call plan (non-stream + stream). An async media case needs at least submit + polls + download. Health checks count toward `max_requests` and cost no spend. A row without finite limits is rejected by the validator, so a case cannot evade the counter. When a case reaches a limit or its wall time with a remote job still open, the lane sends the protocol's cancel request from `max_cancels`, writes the remote job ID to the ledger as `abandoned`, and keeps its reservation (below) until a later run confirms the job's terminal state with one status query or the owner clears it.

**Spend ledger (v3.1; required before any paid case runs).**
- One persistent SQLite ledger outside the repository (`~/.local/state/azizam-live/ledger.sqlite`), written under the host lock. Nightly-live, weekly-live and manual live runs share it.
- `LIVE_SPEND_CEILING_USD` is a calendar-month total in USD, local time, reset on the 1st. It bounds this lane's spend only, not the operator's other use of the same keys.
- Prices are operator data: each paid service × model in `canaries.toml` gives a price per billed unit (input and output tokens, image, second) and any minimum charge, with the date it was checked. A paid case with no price, or with a request whose cost has no upper bound (no output cap the protocol can express), is `SKIPPED` as `UNPRICED`.
- Before each case the lane reserves its worst case in one transaction: maximum input × input price + output cap × output price, at least the minimum charge, times (1 + `max_retries` + fallback attempts), plus priced polls and downloads. If the reservation would pass the ceiling, the case does not start and the lane stops `INCOMPLETE`. It never switches provider to stay inside budget.
- After the case the lane settles the reservation from the upstream's reported cost, else reported usage × price. If neither is known (timeout, crash, no usage), the full reservation stays charged. Settlement is keyed by run, case and attempt, so a duplicate receipt changes nothing. On start, open reservations from an interrupted run stay charged.

**Other rules.**
- Credentials come only from the file passed with `--credentials`; none means paid cases are `SKIPPED`, counted and listed.
- **Model choice (v3.1).** Each case declares its required features (for example stream, tools, usage). The lane picks the cheapest model in `canaries.toml` whose declared features cover them. Size alone does not prove the same contract.
- Response caching is bypassed on both sides (gateway cache: existence UNVERIFIED; upstream cache headers where the provider documents them). A cache hit cannot show current behavior.
- Assertions cover contract and shape only: status, envelope, stream event order, usage, media structure (4.8). Never content. A fixed seed or low temperature is not a reproducibility guarantee. Usage is `required` or `optional` per protocol × capability, from the same registry data as the matrix (4.2.7); a missing required usage is `FAIL`, a missing optional usage is recorded.
- The receipt records per case: service id, model, proxy request counts by kind, upstream statuses, usage, reservation and settled cost, duration, queue wait, outcome.

**Outcomes** are classified from both hops: what the proxy saw upstream, and what the gateway returned.
- `PASS`.
- `FAIL`: contract broken (wrong shape, missing required usage), or a gateway fault: the gateway returns 5xx or a wrong error mapping while the upstream answered 2xx or was never called, or the gateway normalizes a genuine upstream error wrongly.
- `UPSTREAM_UNAVAILABLE`: the upstream itself refused the connection, returned 5xx or 429, or failed the health check, **and** the gateway's response normalized that correctly.
- `SKIPPED`: no credentials, ceiling 0, `UNPRICED`, or not started (1.3 skip rules).
- `INCOMPLETE`: time, request limit or budget ran out.

A `FAIL` opens or updates a `live-red` issue, triaged the same day. The fix is usually a config or transform row (data), sometimes a gateway bug with its own PR and a deterministic regression test in FK or RP. `UPSTREAM_UNAVAILABLE` is listed in the weekly report, not filed, unless it repeats 3 nights in a row.

**Maintenance (v3.1).** The owner (the repository owner, until `canaries.toml` names another) maintains each canary set. Each service row carries `review_by` (≤ 90 days) for prices and credentials. The weekly report lists eligible, executed and skipped cases per protocol × capability with skip reasons, the last `PASS` per case, and rows past `review_by`. A case with no `PASS` for 30 days, or a row past `review_by`, opens a `live-stale` issue. It never blocks signoff. A case with no eligible run for 90 days (its service left `canaries.toml`) is removed in a PR, or kept with a stated reason.

**Quality evaluation** of generated content is not a lane in this design (section 11, G-23).

## 2. Affected-only selection

`scripts/test-affected` (exists) stays the local selector and never feeds the gate. Slice L1 completes its contract:

- **Input.** Committed changes only (`merge-base..HEAD`), as today. This is the documented contract. `--changed-path P` (repeatable) replaces the git diff for mutant replay.
- **Output.** `--json` prints, per framework, `selected` (case or file IDs), `reason` per changed file, or `UNKNOWN` where the framework has no mapping.
- **Exit 2** when any changed production file selects no test ("no test reaches X"). Checked per file, so one selected file cannot hide another orphan.
- **Cross-stack.** A gateway change with no mapped web dependency selects the browser smoke set, not nothing.
- Whole-suite triggers (exist) gain: `tests/support/**` (includes the FK fake upstream and RP transcripts), fixtures no test names by path, `tests/budgets.toml`, `tests/suites.toml`, `scripts/test-affected`. Web: `web/e2e/fixtures/**`, `global-setup.ts`, any Playwright config.

**Recall** (weekly): for each killed mutant, replay the selector with `--changed-path <module>`. A mutant counts as recalled when the selection includes at least one of its killers. Recall = recalled ÷ killed. Each miss adds an edge or trigger and a case in `tests/test_test_affected.py`. Because the gate stays full, a miss slows feedback but cannot merge a defect.

## 3. Flaky policy

### 3.1 Rules

- Zero retries: `retries: 0` in every Playwright config, `retry: 0` in vitest, no rerun plugin in the lock, no shell rerun loop. Receipts record the attempt index; any index > 0 fails `policy`.
- Allowed: locator auto-waiting and polling one condition inside a bounded timeout.
- A flake is a case that passed and failed at one SHA in one environment. One failure is a suspect.
- **Diagnostic repetition is not a retry.** `scripts/test-repeat <case-id> N` (built with Q) writes its own receipt. It never changes a gate or nightly result, and a failed first attempt stays recorded. It may turn on Playwright video and full tracing for the repeated case. Until Q builds it, the manual equivalent is Playwright `--repeat-each N --retries=0` on the one case, or a shell loop over one pytest node ID, run by hand outside every gate and nightly; its output is diagnostic, not a receipt.
- With zero retries every recorded run is a first attempt, so the first-attempt failure rate is the failure rate in `cases.jsonl`. It counts deterministic regressions as well as flakes. The weekly report lists, per case, first-attempt failures over the last 100 runs and the wall minutes they cost, and separately the confirmed flakes (as defined above).

### 3.2 Until quarantine exists

No quarantine tooling is built in advance. `policy` fails on any quarantine manifest or marker. A flake is fixed, or the change that introduced it is reverted (1.3). Build slice Q (3.3) when a flake has no fix within 2 days.

### 3.3 Quarantine contract (slice Q, on demand)

- **Source:** `tests/quarantine.json` only; no markers in test code. Fields: owner, issue, reason, failure signature, first-failure receipt, `expires_on` ≤ 14 days (expired fails the gate).
- **Identity:** framework + config/project + full collected case ID (pytest node ID with parameters; vitest file + title path; Playwright config + project + file + title path). `policy` resolves each entry against the collected lists; zero or several matches fail, so file and suite entries fail. The runner removes exactly that case (pytest `--deselect`, Playwright `--test-list`, vitest a setup hook; the vitest mechanism is UNVERIFIED). Nightly still runs it.
- **Protected invariants:** for a case in `tests/critical.txt` (the 13 sensitive files, auth, privacy, media ownership, isolation), the entry also names the `invariant` and a gate `replacement` case, with a receipt where the replacement fails on a named mutant or revert and passes clean.
- **Exit:** the fix names the mechanism (clock, barrier, shared state, port) and adds a regression check. Then 30 first-attempt passes at the fix SHA (10 alone, 10 in its file, 10 in full random-order runs, ≥ 3 seeds), each receipt with seed, order digest, environment and SHA. Never widen a timeout or add a sleep to pass.
- **Negative tests:** ambiguous or zero match, file-scope entry, critical case without replacement, missing or wrong-SHA exit evidence, expired entry, runtime retry. Each must fail `policy`.

### 3.4 Browser determinism rules (v3)

These apply to new and moved browser code at once, and to existing code through the ratchets in section 6.

1. **Web-first assertions.** Assert with `await expect(locator).toBeVisible()`, `toHaveText()`, `toHaveURL()` and similar. No one-shot `expect(await locator.isVisible())`, `textContent()`, `count()` or bare `.isVisible()` as an assertion.
2. **No fixed waits.** No `waitForTimeout`, no `waitForLoadState('networkidle')`. Wait for the observable outcome. Time-driven UI uses `page.clock` or `context.clock` (exist in a few specs).
3. **No forced actions.** No `force: true`. A control that looks enabled before hydration is a readiness defect in the app; fix the app.
4. **Real controls.** A journey test activates the real UI control for the action under test. It never calls the API to perform that action. A broken button must fail the test.
5. **Separate outcomes.** For media jobs, assert acceptance, progress, terminal state, preview readiness and download availability as separate steps. Never wait for a presumed generation time.
6. **Stable locators.** New and moved code uses role, label or an explicit `data-testid`. Existing DOM-shape locators are changed only in files a slice already edits.
7. **Isolated data (BI2).** A test that changes server state gets its own namespace from the `isolated` fixture. Read-only tests may use the lane seed. Each lane keeps its own gateway, fake upstream, port pair and fresh state directory (exists, `serve.py`).
   - **Namespace.** `<run-id>-<lane>-<project>-<worker>-<invocation>`, where `<invocation>` is a counter unique per test invocation in the run, so two copies of one case in one lane differ. The fixture writes the namespace → case ID, seed and repeat index mapping to `cases.jsonl`, so a failure can be reproduced.
   - **Principals.** A test that changes principal-scoped state (sessions, keys, quotas, rate limits, preferences) gets its own member principal and key, created through the admin API, never the lane owner. Quotas and rate limits then belong to that principal, so a test never spends another test's quota.
   - **Lane-global state.** A test that changes owner-only or global state (gateway settings, service registration, the owner session) declares `globalState: true`. Those tests run in a serial group, after which the fixture restores a recorded snapshot of the touched settings through the admin API, and the next test checks the snapshot. Where restore is not possible, the test runs on its own lane instance.
   - **Fake state.** FK scenario state, request logs and cursors are keyed by the namespace (4.6), so two invocations never share a scenario cursor.
   - **Teardown.** The fixture fails the test if its namespace still has a pending job, an in-flight upstream request or an unconsumed required scenario step.
8. **Layer label.** Each spec file declares one layer in `tests/suites.toml`: `intercept` (`page.route` stubs the gateway API; proves the UI's handling of controlled responses), `gateway-fake` (real gateway, FK upstream; proves gateway transport and orchestration too), or `live` (1.4 only). The critical journeys in S14 must exist at the `gateway-fake` layer.
9. **Egress (BI1).** Permitted: the lane's web and gateway origins on `127.0.0.1` at its two ports, plus `data:` and `blob:`. Two layers:
   - Chromium starts with `--host-resolver-rules` that map every host except `127.0.0.1` and `localhost` to `NOTFOUND`. This covers redirects, service workers, WebSockets and contexts the test fixture did not create.
   - The test fixture routes `**/*` on every context it creates and sets `serviceWorkers: 'block'`; a request to any other origin is aborted and recorded in a per-test violation latch. Teardown fails the test on any latched violation, so a request the app swallowed still fails its test. WebSockets to other origins are latched through `page.on('websocket')`.
   - A deliberate external-origin test lists its origins in the fixture option `externalOrigins`; the fixture fulfils them with a stub and records them, and they do not trip the latch.
   - Server side: the lane gateway's upstreams are FK on loopback. BI1 adds one negative test (a service registered at a non-loopback address is not reached). If no server-side boundary exists, BI1 states this in its PR and section 10 keeps it as UNVERIFIED. Any network path BI1 does not test (for example WebRTC) is listed UNVERIFIED.
10. **Evidence.** Gate configs keep `trace: "retain-on-failure"` and `screenshot: "only-on-failure"` (exist; the sweep config has both off). Video stays off in the gate. A failed case's `cases.jsonl` row links its trace, screenshot, the lane's gateway and fake-upstream logs, browser version, SHA and the gateway request and job IDs it saw. Artifacts stay in the receipt directory and are never uploaded.
    - **Lane logs (BI1).** `serve.py` cannot know the browser result; it only receives a shutdown signal. So `serve.py` writes lane logs into `<receipt run dir>/lanes/<lane>/` (directories 0700, files 0600) instead of a `TemporaryDirectory`, and the runner decides after the lane's Playwright report is final: it keeps the logs of a lane with any failed case, setup failure or interrupt, and deletes the rest. Retention follows the receipt directory (90 days, 20 GiB, 1.3).
    - **Raw and summary.** Raw lane logs are restricted: they may hold key material. The runner also writes a summary per failed case (request IDs, methods, paths without query strings, statuses, job IDs and transitions, with `Authorization` values and key prefixes masked). Only the summary may be quoted in a PR or issue; raw logs are never copied there.
11. **Visual comparison.** No `toHaveScreenshot` gate in this design (section 11, B-22). Never compare generated model output pixel for pixel.

## 4. Oracles

Rule for all areas: mock the process boundary (HTTP transport, clock, subprocess, randomness), not our own functions; call production code; assert the complete value. The 725 `patch.object(proxy, …)` uses are a ratchet: the count may fall, never rise.

Parsing rule (v3): the gateway parses upstream responses tolerantly (unknown fields are ignored) and normalizes its own output strictly. Tests assert the complete normalized output, and each protocol fake has an `extra-fields` scenario that adds unknown fields upstream and expects unchanged output.

Priority: first repair self-fulfilling oracles inside the slice that owns them (M3 real-serializer check, M1 audit paging and M4 receipts on real stores, privacy regressions). Then small stable goldens (catalogs, error envelopes). Broad OpenAPI and protocol transcripts come last.

### 4.1 Golden files

One harness: `scripts/golden --check [surface]` in the gate; `--update` by hand only, and a changed golden is reviewed in the PR diff. Sorted-key JSON, fixed synthetic inputs (clock, IDs, origin `https://gateway.test`). Normalizers name each field path they change; no regex scrubbing. Privacy canaries run on the raw response before normalization. Two generations must be byte-identical. On failure the harness writes the actual output and the diff into the receipt directory.

G1: model catalogs (`/v1/models`, `/anthropic/v1/models`, `/me/api/models/catalog`, each allowed and denied), `/.well-known/azizam` per audience, error envelopes per surface. G2: full `app.openapi()` (minimal and full fixture), Anthropic and Ollama transcripts, receipt projections per member. Kept: `test_transforms_golden`, `test_lifecycle_swap_golden`, `dispatch_admission_baseline.json`, the comfy object-info catalogs.

### 4.2 Borrowed conformance and differential checks

1. **Gate (B1):** record each schema fixture's upstream URL, SHA, license and date (`openai-openapi-responses*.json`, `anthropic-sdk-schemas.json`, `ollama-api-schemas.json`, reproducible by `tests/fixtures/generate_*`). Fix M3: `test_openai_schema_conformance` validates the gateway's own serializer output.
2. **Gate (S7):** official OpenAI, Anthropic and `ollama` Python clients over `httpx.ASGITransport`. SDK parsing proves interoperability only, so each case also asserts normalized content, tool calls, usage and the ordered event list.
3. **Gate, differential pairs:** Anthropic vs OpenAI route on one fake engine; category-policy preview vs `decision_for`; `runtime_auth` hash equals `proxy.hash_api_key`; the LiteLLM fake vs the real `LiteLLMAdminClient` contract.
4. **Scheduled (weekly), then gate:** MCP conformance (`modelcontextprotocol/conformance`, pinned). Promote only when its measured runtime fits the gate budget and it catches at least one seeded defect. Upstream compatibility today: UNVERIFIED.
5. Not adopted: A1111 upstream tests (AGPL). vLLM, llama.cpp and LiteLLM suites are mined for single wire assertions.
6. **Scheduled (weekly), then gate (v3, from G2):** Schemathesis, pinned, against the gateway's own `app.openapi()` on a disposable instance with FK upstreams. Never against a paid or live endpoint. Promotion follows the rule in item 4. Its fit with our routes and its runtime: UNVERIFIED.
7. **Gate, protocol × capability matrix (v3, S6/S7):** one conformance case set, parametrized by the registry's own `protocol` values and `capabilities` enum (data, read from the registry module, not listed in the test). Each cell either runs or names why it does not apply. `policy` reports executed, skipped, pending and missing cells from `cases.jsonl`, so a capability flag cannot hide a gap. The same cases run against three sources, and each source has its own required cell set in `tests/matrix.toml`, declared per milestone:
   - **FK (gate):** from S6/S7, every registry protocol × capability cell runs or names why it does not apply.
   - **RP (gate):** only cells with a reviewed transcript run (from RP2: OpenAI-compatible chat). Every other cell is `pending` with owner, reason and `expires_on` ≤ 90 days; an expired `pending` fails `policy`, and a renewal states why in its PR. A pending cell is never reported as executed.
   - **Live (1.4):** chat cells from LV1, media cells from LV3; others `pending` on the same terms.

### 4.3 Property-based (Hypothesis, in the hermetic lock)

The reference is an independent model where one exists, otherwise a metamorphic relation. Copying production logic into the test is not an oracle. Gate: 100 examples; nightly: 2,000 with a fixed seed; shrunk counterexamples stay as explicit examples.

| Slice | Module | Property |
|---|---|---|
| P1 | `category_policy`, `category_enforcement`, `policy` | Relational grant/deny/unknown model; the overlay never widens; tightening an allow-list never grants; the first failed guard decides. |
| P2 | `public_failure` | See below. |
| P2 | `transforms`, `speech_voices` | Pipeline = ordered composition; defaults fill only missing leaves; catalog agrees with value-map resolution. |
| P3 | `media_jobs`, `receipt_store` | State machines on a real temporary SQLite store: legal transitions, replay window, one terminal finalize, a duplicate completion callback changes nothing, cancellation reaches the upstream fake. |

**`public_failure` (verified on this branch).** `describe()` formats the template with caller values. `PublicFailure.body()` returns `details` uncopied and unfiltered, so an unused detail (for example `secret="CANARY"`) appears verbatim in `error.details`. A caller `layer=` overrides the computed layer inside `details` while `self.layer` stays computed. A missing placeholder value raises `KeyError`. The module docstring tells developers to put raw data in `details`, so pass-through may be intended.

P2 characterizes these four behaviors on the whole raw envelope with nested canaries, and asks the owner to state the public policy (which `details` keys reach which audience; may `layer` be overridden). Until then nothing is called a defect and no rewrite strips `details`. The "every key yields a layer and message" property covers only valid inputs: each copy key with string values for exactly its placeholders. *Exposure* (the envelope carries caller data) is verified; *reachability* (a public route puts secret-bearing values into `details`) is UNVERIFIED and gets a route characterization test. A confirmed leak gets its own bug-fix PR.

The other dot gaps (`default-params`/`value-map` share mutable config; `voxtral-voices` maps unknown voices to `alloy` while reporting `allow_other=False`; legacy `record` can overwrite a terminal receipt) follow the same rule.

### 4.4 Real dependencies in place of mocks

**Where test databases live today (static reading of `run-hermetic-pytest.sh` 51-97 and `tests/conftest.py` 31-54; S0 confirms with `stat -f` inside the sandbox):**

| Data | Path | Backing |
|---|---|---|
| Import-time stores (`admin.db`, `metrics.db`, `receipts.db`, `media_jobs.db`, …) | `$state_dir/` and `$state_dir/xdist-<w>/` | Sandbox `--tmpfs /`. Already RAM. |
| pytest basetemp, `tmp_path`, and fixture databases under it | `$TMPDIR` = `$state_dir/tmp`, bound from the host | Host disk (`$HOME/.tmp`). This is the fsync cost. |
| Checkout, venv | read-only binds | Host disk; not written. |

The measured 40 s run moved **all** of `$HOME/.tmp` to `/dev/shm`, including checkout, venv and uv cache. It does not isolate the cause.

**S0, the change:** bind `$state_dir/tmp` from a tmpfs source (`/dev/shm/azizam-pytest.XXXX`), with a free-space check before the run and cleanup on exit. If the check fails, the run uses disk and the receipt says so. No production pragma changes.

**Disk-backed routing.** No test today needs real fsync: a process kill keeps committed pages in the page cache on tmpfs too, and the tmpfs run passed all 7,884 tests. A test that needs host-disk semantics (free space, device boundaries) requests a `host_disk_dir` fixture backed by a separate bind of `$HOME/.tmp/azizam-pytest-disk.XXXX`. A marker alone moves nothing, so there is no `real_fsync` marker.

**S0 A/B:** 5 runs each of disk and tmpfs `TMPDIR`, same SHA, same worker count, load recorded. Report wall, CPU, test-seconds, setup share, peak tmpfs use and the backing of each path above. Two random-order full runs show no cross-test leak. Serial and xdist runs both pass, and the tmpfs directory is gone after exit. Until this A/B, every S0 saving is UNVERIFIED.

**After S0, only if the profile still shows setup dominates:** template databases (migrate once per session; copy the file only after every connection is closed and the WAL is checkpointed; keep explicit fresh-migration tests) and module-scoped apps for read-only cases.

Other replacements, each inside its owning slice:
- Real stores: admin audit paging (M1) on the real keyset SQL, receipt persistence (M4) on the real store. `FakeAdminStore` stays only for router wiring.
- Loopback ASGI fakes at the HTTP edge (LiteLLM, IdP `/token` + `/jwks`, engine) replace patches of `proxy` internals in the sensitive and route-policy files. They live in `tests/support/fake_upstream/` on the FK1 core; S2 adds the LiteLLM and IdP edges there (4.6).
- Clocks and barriers replace outcome-deciding `sleep(` calls; `page.clock` replaces the 75 s and 30 s browser waits. Retry and backoff schedules run on a virtual clock; each retrying path keeps one case with a real, short timeout to prove the timeout is wired.
- `tests/support/` replaces the cross-test imports of `test_admin_oidc`, `test_exposure_oidc_security`, `tests.test_admin_api`.

### 4.5 Mutation testing

- `scripts/mutate <module>` (T0, from the audit harness) refuses to score unless its baseline run is green. It writes each mutant's ID, status and killer case IDs.
- Core modules: `category_policy`, `category_enforcement`, `policy`, `public_failure`, `speech_voices`, `transforms`, `media_jobs`, `receipt_store`, `admin_store` (auth paths), `runtime_auth`.
- Survivors are classified (killable, equivalent, unproductive) in a repo file. Score = killed ÷ (all − equivalent).
- Uses: named mutants for the slice ledger (section 7); the weekly report; final milestones. Never a gate.

### 4.6 Fake upstream (v3, slices FK1, FK2 and the owning slices)

One package, `tests/support/fake_upstream/`, serves every deterministic upstream the tests need. It is built in steps: FK1 builds the core with one protocol and one consumer; FK2 moves the browser fake onto it; each later protocol, edge and scenario arrives with the slice that first uses it.

- **One app per wire protocol** the registry accepts (grep finds at least `openai-compatible`, `anthropic`, `ollama` and `comfyui` in `gateway/`; the full list is read from the registry, UNVERIFIED here), plus the non-provider edges S2 needs (LiteLLM admin, IdP `/token` + `/jwks`). FK1 builds `openai-compatible`; S2 adds the edges; S6/S7 add the other protocols. It grows a protocol only when the registry does.
- **Scenarios are data.** A scenario is a JSON file: request matcher, ordered response steps (status, headers, body or SSE chunks, delays on the virtual clock, disconnect), and which steps are required. Code branches on protocol, capability and step type only. A registry protocol value used as a discriminator is data, even when it shares a name with an engine.
- **Engine-knowledge guard.** The guard's scan (as in `gateway/`) covers this package, `tests/live/`, `scripts/record-upstream` and the RP normalizers: an engine name in a branch fails.
- **Engine quirks.** A test for an engine quirk is a scenario row that reproduces the quirk plus the transform config row (`config["transforms"]`) an operator would add. No fake subclass per engine.
- **Registration.** Pytest wires the fake through `httpx.ASGITransport` or a loopback port; browser lanes run it as the lane's upstream process and register it through the admin API, as `global-setup.ts` already does for `capture-engine`. FK2 replaces `scripts/ui/fake_engine.py` behind the same seed rows.
- **Which transport proves what (v3.1).** ASGI proves application semantics: status mapping, normalization, retry decisions, usage. Wire properties need the loopback port mode: connect refused, connect and read timeouts, SSE chunk boundaries on the wire, disconnect mid-stream. A transport fault case runs on loopback; an ASGI-only test never claims transport fidelity.
- **Virtual time (v3.1).** In ASGI mode and in the in-process loopback mode, the fake's delays are steps on the test's clock fixture, which the gateway under test also uses, so one clock advances both. A fake in a separate process (browser lanes) uses short real delays bounded by the scenario. Each retrying path keeps one real-clock wiring case (4.4).
- **Fail closed (v3.1).** An unmatched request gets HTTP 599 with the request in the body, but the 599 is not the oracle, since the gateway may absorb it through error handling or fallback. The fake also records each violation (unmatched request, call out of order, call past a step's count) in a per-test latch. A teardown fixture fails the test on any latched violation, on any required step not consumed, and on a call count or order other than the test declares. A negative test proves an unexpected call fails its test even when the gateway catches the 599. Tests assert the forwarded request (model, parameters, job ID), not only the response.
- **Scenario catalog,** each entry added by the slice whose consumer first uses it, never ahead of it:
  - LLM (FK1 the basic text and stream rows; S6/S7 the rest): text, structured output, refusal, tool calls, empty output, truncated JSON, SSE event split across chunks, malformed chunk, disconnect mid-stream, `extra-fields`.
  - Image (S5a/S5b): bytes, artifact URL, wrong MIME, wrong dimensions, corrupt download, expired URL.
  - Jobs (S4+P3, S5a/S5b): queued → running → completed, failed, slow polling, cancel during run, duplicate completion callback.
  - Speech (S5a/S5b): valid audio, silent audio, wrong sample rate, partial stream.
  - Every protocol (S6/S7): 401, 429 with `Retry-After`, 5xx, connect timeout, read timeout, fallback to the next backend, usage reporting, bounded retries. Combinations of faults run below the browser only.
- **Fake drift.** A fake shows only what we believe the protocol is. RP transcripts (4.7) and live canaries (1.4) check that belief. A scenario copied from a transcript names it in its provenance field.
- **Media bytes.** Fixed tiny files (exists in part: `tests/media_test_bytes.py`) with recorded MIME, dimensions, duration, sample rate and SHA-256.
- **Migration.** FK2 moves `scripts/ui/fake_engine.py`. S6/S7 move `tests/compat/harness/fake_backends/`. Other ad-hoc fakes (59 files) move inside their owning slices. Each migration PR leaves both the old and the new consumers green. The count is a ratchet.

### 4.7 Replay transcripts (v3, slices RP1, RP2)

A transcript is a recorded exchange with a real upstream, replayed by FK as a scenario. It catches shapes the fake author did not imagine. It cannot show the upstream still behaves that way; 1.4 does that.

- **RP1 (deterministic, no real capture):** the `canaries.toml` schema, validator and example (1.4); `scripts/record-upstream`; allowlists; normalizers; the `policy` secret scan; replay. All tested against FK standing in for a real upstream.
- **RP2 (manual capture):** the owner records the first reviewed transcripts for OpenAI-compatible chat. Running the recorder against a real upstream needs the owner's explicit authorization each time; no agent or timer runs it.
- **Recording** is by hand only: `scripts/record-upstream --service <id from canaries.toml> --set <request set>`, run by the owner against the upstream directly, never through ports 5000/15000. The request set is committed and synthetic, so no real prompt or upload is recorded.
- **Kept data.** An allowlist of fields and headers per protocol; everything else is dropped. Hosts and signed URLs are replaced by path-named normalizers (as in 4.1). Binary media is stored apart from the metadata, under the media-bytes rules of 4.6.
- **Size.** `policy` enforces ≤ 256 KiB of metadata per transcript and ≤ 20 MiB for the whole corpus including media.
- **Scan before commit.** `policy` scans transcripts and fixtures for key prefixes (`opk_`, `oak_`, `sk-`), bearer tokens, home paths and LAN addresses, and fails on a hit. Scrubbing is incomplete by nature (`testing/record-replay-test-tools-treat-cassette-scrubbing…`), so the allowlist and the synthetic request set carry the load and the scan is a backstop.
- **Provenance** per transcript: service protocol, engine and version as reported by the upstream (data), date, request-set hash, recorder version, owner, and `review_by` (≤ 180 days after recording).
- **Replay** is strict: method, path, allowlisted headers and the normalized semantic body must match, including model, parameters and job IDs. A miss fails (4.6). There is no passthrough mode.
- **Staleness.** The weekly report lists each transcript's age, its `review_by`, and the last live result for that service. A transcript past `review_by` opens a `live-stale` issue (1.4); the owner re-records it in a reviewed PR with the diff of the normalized transcript, or deletes it and marks its cells `pending`. Nothing refreshes automatically.
- **Tool.** Not VCR.py and not Polly.js (section 11, G-7, G-8).

### 4.8 Media oracles (v3, S5a/S5b)

- **Gate, every media result:** decodes; MIME; pixel dimensions, or duration, sample rate, channels and frame rate; not empty and not silent where content is expected; artifact count. Decoding uses the pinned toolchain in the receipt fingerprint (ffmpeg).
- **Exact SHA-256** only for fixture integrity and deterministic gateway-side transforms (for example a pass-through or a fixed re-encode with pinned tools).
- **Perceptual distance** (dHash/pHash, audio fingerprint, VMAF) only where the gateway itself changes media deterministically and a byte hash is too strict. A threshold is set from measured good and deliberately bad outputs and recorded with them. Today no such case is known (UNVERIFIED), so none is built in advance.
- **Never** a perceptual or content check on generated output in the gate or in 1.4.
- On failure the harness stores the actual media and a diff image or report in the receipt directory.
- Cost: the decoded properties of an immutable fixture file are computed once per session and cached by its SHA-256. Results the gateway produced are always decoded fresh.

## 5. Deletions, merges, moves

Each row needs its ledger line (section 7) before merge.

| Item | Action | Reason |
|---|---|---|
| DD1-DD21 (`consolidated.md` 2.1) | Delete | Duplicate; the named survivor kills the named mutant. |
| DA1-DA9 | Replace | Asserts nothing (tests a copy of production logic or its own socket). |
| `test_auth_inject` first-match test; `test_lan_sk_local…`/`test_unknown_virtual_key…`; two pairs in `test_litellm_key_endpoints`; sign-in provider-label rows | Delete or parametrize | Duplicate (`sensitive-audit.md`). |
| Cookie-forging half of `test_sec08_existing_owner…` | Delete | Duplicate of the real-callback test in `test_sec_auth_login.py`. |
| 14 `*.e2e.ts`, `app-shell.playwright.spec.ts` | Move unique assertions into gated `web/e2e/*.spec.ts`, then delete the copy and its area config | Run nowhere; the copies diverged. |
| `web/tests/ux/services-route.spec.mjs` | Delete | Replaced by `services-dialog.spec.tsx` :84/:121; no script runs it. |
| `foundation`, `phone-overflow`, `public-surfaces` matrices (170 cases) | One route × theme × width matrix; gate runs desktop-light + phone-dark, nightly all | Duplicate overflow checks. |
| Home, health-pill, status, provider-unknown, owner-alert, settings clusters | One vitest truth table per concept + one browser seam | Display states repeated in the browser. |
| Browser HTTP-only assertions (headers, redirects, range, tamper) | Move to API tests | The browser adds nothing. |
| `test_admin_render` JS/CSS strings; `models-client.spec.tsx` :1704 | Move to vitest DOM or browser | Pass when the rendering is dead code. |
| `test_user_render` :726 skip without Node | Fail instead | Silent skip in the gate. |
| `probe_openai.py` remote probes, `tests/compat/node/*` | Move to scheduled (1.3) over FK upstreams | Need a running instance. Real-upstream variants go to 1.4. |
| `scripts/ui/fake_engine.py`, `tests/compat/harness/fake_backends/` (v3) | Move into `tests/support/fake_upstream/` (FK2 and S6/S7) | Three fakes of one protocol drift apart. |
| Browser cases that vary provider, model or parameter combinations or API errors (v3) | Move to API or vitest tests unless the UI shows a distinct state | Combinations belong below the browser. |

Not merged (different seams): the redaction layers, swap drain unit vs lifecycle, the prompt-memory family, origin-bound secret layers.

## 6. Budgets

Every target is provisional until measured on this host at the recorded load. Time is observational: the weekly report shows a rolling p50 and p95 over the last 20 gate runs, with the sample size and the end-to-end critical path (lock wait, compose, Python, browser lanes); no gate fails on time. A p95 from fewer than 20 runs is not reported.

**Count units:** pytest collected items (each parameter set counts; subtests reported, not counted); vitest test cases; Playwright test × project. Quarantined cases count before deselection, so quarantine cannot buy budget.

**Count ceilings** in `tests/budgets.toml`, enforced by `policy` from T0. T0 sets them at today's counts. A slice that must add cases (S13 adds ≤ 40 browser cases; FK1 and S6/S7 add the protocol × capability matrix) raises the ceiling in the same PR to a stated integer, computed from the matrix dimensions (protocols × capabilities × sources × variants) and reviewed there; S14 lowers it. No other raises, and no open-ended exception.

**Cost delta per slice (v3.1).** Each slice PR records, against the gate before it: collected-case delta, test-second delta, matrix dimensions, setup cost and slowest-lane delta. Recorded delays in transcripts and scenarios run on the virtual clock. Whether the 360 s gate target is reachable with the v3 additions is UNVERIFIED until these deltas are measured.

| Tier | Today (measured) | Provisional target |
|---|---|---|
| Python gate | 7,891 collected + 2,803 subtests; 287 s wall (229-325); 2,430 test-s | ≤ 8,000 collected, plus the integer matrix raises of FK1 and S6/S7; wall and test-s set from the S0 A/B |
| Vitest | 1,467 tests; 19 s (16-20) | ≤ 1,800 (browser states move here); ≤ 30 s |
| Browser gate (main + live-data + household) | 737 + 4 + 15 = 756 cases; main 363 s on 6 measurement lanes, slowest lane 279-352 s | ≤ 560 cases after S14; slowest lane ≤ 240 s |
| Gate wall | 421-488 s (before S0) | p50 ≤ 360 s, reviewed after S0 and S14 |
| Local loop | UNVERIFIED | p50 ≤ 90 s gateway, ≤ 30 s web |
| Scheduled | none | nightly 60 min, weekly 3 h, per-task allocations (1.3) |
| Live (v3) | none | nightly-live 10 min, weekly-live 20 min, paid spend 0 until the owner sets a ceiling (1.4) |
| Ratchets | 725 `patch.object(proxy…)`; 114 `sleep(` (upper bound); 6 gate skips; 16 no-gate files | never rise; no-gate files 0 after S13 |
| Browser ratchets (v3, grep counts, T0 recounts) | `waitForTimeout` 19; `force: true` 4; `networkidle` 12; one-shot `expect(await …)` 11; bare `.isVisible()` 6; inline `page.route` bodies UNVERIFIED (≤ 329 calls) | never rise; the first five reach 0 by the end of S14 |
| Fake ratchet (v3) | 59 test files with their own HTTP app/server; 14 with `httpx.MockTransport` | never rise; fall as owning slices migrate to FK |

Per-lane receipts (v3, T0 addition) also record webServer start and seed time, test time, and a load sample, so the slowest lane and the setup share are visible before S14 changes lanes or workers.

## 7. Slices

One PR per slice, rebased on the current release, files re-read there.

**Proof for every slice:**
1. One fresh full exact-candidate gate with signoff (1.2).
2. Focused red/green: each new or rewritten test fails on a named mutant or revert, then passes on clean code.
3. A ledger in the PR description, one row per touched invariant:

   | Old case | New case (tier) | Invariant | Kind | Mutant / revert | Old on it | New on it | Clean receipt |
   |---|---|---|---|---|---|---|---|

   - *Preserved:* old and new both kill. Renames, moves, parametrization and additions are allowed when the row maps them.
   - *Strengthened:* old survives, new kills.
   - *Deleted:* the named survivor kills; the reason is stated.
   - For touched core modules, `scripts/mutate` before and after lists lost, added and equivalent kills by mutant ID. A lost kill is fixed or justified as equivalent. A score alone is not evidence.

**Extra for slices that change shared fixtures, isolation or runner state (S0, `tests/support/`, FK1, FK2, BI2a and each BI2 batch, Q) and for milestones:** three full random-order runs with recorded seeds, and the mutation diff over all core modules.

| # | Slice | Extra proof |
|---|---|---|
| F0 | `job-notices.spec.tsx` flake fix. Landed in `a97c0ce7`. | 0 failures in 20 shuffled runs under load (receipt not checked by this revision) |
| T0 | Prerequisites (below). In flight as T0a. | Each `policy` self-test fails on injection; signoff refuses in each case of 1.2; two seeds give identical item order on every xdist worker |
| S0 | `TMPDIR` on tmpfs, `host_disk_dir` fixture. v3.1: `_hermetic.py` gains a loopback-address check for ports allowed by a wildcard bind, and its negative tests | The A/B in 4.4; the same port at a non-loopback address fails; a UDP send and a native child's connect fail inside the namespace; the runner installs the hook (asserted from inside a test) and it is restored after |
| L1 | Selector contract (section 2) | Exit 2 on a per-file orphan; `--changed-path` replay matches git-diff selection |
| S1 | DD1-DD21, DA1-DA9. Landed on `main` (`83189ad1`, `eef202cb`, `aac0d808`). | Ledger rows from `consolidated.md` 2.1 |
| S13 | Orphaned browser files. In flight; v3 adds: moved code follows 3.4 rules 1-6 and must not raise the browser ratchets; each moved spec gets its layer label. | Per moved assertion, a UI mutation fails the gated spec; ceiling raise ≤ 40 |
| S3+P1 | Enforcement, route policy; P1 properties. In flight; unchanged by v3. | S3 mutants; `_report` fails on 400/422/500 |
| BI1 (v3) | Browser guards: the two egress layers and violation latch (3.4.9); layer labels in `suites.toml` for every spec; eslint-plugin-playwright (pinned) or a `policy` scan for the five hygiene patterns, as ratchets; lane logs in the receipt run directory, kept by the runner for failed lanes, with per-case summaries (3.4.10). If the labels or ratchets need edits to spec files, they split off as BI1b. | An external request fails its test even when the app swallows the error, from a page, a second context, a redirect and a service worker; an `externalOrigins` stub does not trip the latch; the server-side negative test (3.4.9); an added `waitForTimeout`, `force: true` or one-shot assertion fails `policy`; a failed browser case whose servers exit normally keeps readable 0700/0600 logs and its row links them; a passing lane's logs are deleted |
| BI2a (v3.1) | `isolated` fixture (3.4.7): namespace with invocation counter and mapping, member principals, `globalState` serial group with snapshot restore, teardown checks; inventory of state-mutating spec files grouped into named batches, written to `tests/suites.toml`; one pilot spec family migrated | Two copies of one pilot case in one lane, in parallel, do not interfere; a test that leaves a pending job or touches an unrestored setting fails teardown; the pilot passes alone, in its file and under two lane-plan seeds |
| FK1 (v3.1) | Fake upstream core (4.6): package, scenario format, matcher, violation latch and teardown checks, request log, ASGI and loopback modes on one clock, guard scan extension; the `openai-compatible` app with basic text and stream scenarios; one Python consumer migrated | An engine-name branch fails the guard; an unexpected call fails its test even when the gateway catches the 599; an unconsumed required step fails; a split SSE chunk on loopback is reassembled; the migrated consumer's old and new tests are both green |
| FK2 (v3.1) | Browser lanes move from `scripts/ui/fake_engine.py` to FK behind the same seed rows; the old file is deleted | Every browser lane passes on FK with unchanged seed rows; per-scenario ledger rows map each `fake_engine.py` behavior to its FK scenario |
| S2 | Admin plane on the real store (M1, M4); sensitive rewrites over loopback fakes; adds the LiteLLM admin and IdP edges to FK | Audit-paging and credential mutants; revoked key gives 401 through the loopback |
| B1 | Schema provenance, M3 fix | A renamed serializer field fails the schema check |
| P2 | `public_failure`, `transforms`, `speech_voices` | The four characterizations in 4.3; owner policy recorded |
| G1 | Golden harness: catalogs, discovery, error envelopes; failure diffs into the receipt directory | An added field, a reordered list and a leaked canary each fail |
| RP1 (v3.1) | Replay machinery (4.7): `canaries.toml` schema, validator and example; `scripts/record-upstream`; synthetic request sets; allowlists; normalizers; size limits; the secret scan in `policy`; strict replay. No real capture. | A planted key, home path or LAN address fails `policy`; a changed model or job ID in the request fails replay; recording against FK as a stand-in yields a transcript that replays green; a `canaries.toml` row without finite limits fails validation |
| RP2 (v3.1) | First reviewed transcripts for OpenAI-compatible chat, recorded by the owner with explicit authorization; RP cells in `tests/matrix.toml` | A transcript-only shape (one the hand-written fake lacked) is covered by a case; the scan passes on the committed transcripts |
| S4+P3 | Prompt memory, job recovery; state machines, including duplicate callback and cancellation (4.3) | S4 mutants; barriers replace sleeps |
| S10, S11 | Receipts and spend, including usage coherence between upstream usage and the receipt; Python renderers | S10/S11 mutants |
| S6, S7 | Execution, providers; conformance, compat; SDK differential clients; the protocol × capability matrix (4.2.7): every FK cell, RP cells where a transcript exists and `pending` elsewhere; the other FK protocol apps and the cross-protocol fault scenarios; `tests/compat/harness/fake_backends/` moves to FK; virtual-clock retry schedules plus one real-timeout case per retrying path | Forwarder-removed and DONE-ignored mutants fail, not skip; a removed capability cell shows as missing in `policy`; an expired `pending` cell fails `policy`; each fault scenario has a case that fails when the gateway's handling of it is reverted |
| LV1 (v3.1) | Live runner, deterministic (1.4): `tests/live/`, counting proxy, request limits, spend ledger, model choice, two-hop outcome classifier; chat cases only. Tested against FK posing as the real upstream; no timer, no real call | Each outcome rule of 1.4 has a case: a healthy upstream with a gateway 500 gives `FAIL`; a real upstream 503 normalized correctly gives `UPSTREAM_UNAVAILABLE`, normalized wrongly gives `FAIL`; credentials absent gives `SKIPPED`, counted; an unpriced paid case is `SKIPPED`; a reservation past the ceiling stops the lane `INCOMPLETE` before any call; a crash after reservation keeps it charged on restart; a duplicate settlement changes nothing; a request past a limit is refused by the proxy; no gate step imports `tests/live/` |
| LV2 (v3.1) | Live rollout: `nightly-live` and `weekly-live` tasks, completion-triggered units, `live-red` and `live-stale`, weekly-report totals. Enabled by the owner; paid cases stay off until the owner sets the ceiling | Receipts show queue wait apart from run time; a nightly that overruns makes `nightly-live` record `SKIPPED` with the reason, not green |
| S8, S9, S5a, S5b, S15 | Lifecycle, manifests, media (structural oracles of 4.8 on decoded pixels/PCM), platform; the image, job and speech FK scenarios | Per-slice mutants in `consolidated.md`; a wrong-dimension and a silent-audio FK scenario each fail |
| LV3 (v3.1) | Live media cases, after the 4.8 oracles exist; async request limits per capability in `canaries.toml` | On FK posing as upstream: a submit → poll → download sequence passes within its limits, and exhaustion at each step (submit, poll, download) stops the case `INCOMPLETE`, sends the cancel and records the job as `abandoned` |
| S12a-c | Vitest oracle rewrites | Classifier and seek mutants |
| G2 | Full OpenAPI, protocol transcripts, receipt projections; intercepted JSON in `web/e2e/fixtures/` validated against `app.openapi()`, inline `page.route` bodies as a ratchet; Schemathesis weekly (4.2.6) | As G1; a fixture with a removed required field fails |
| BI2b… (v3.1) | One PR per named batch from BI2a's inventory: its specs move to the `isolated` fixture, with hygiene fixes in the files it touches. Batches may land any time after BI2a; all land before S14. The last batch turns on the nightly's second lane-plan seed (1.3) and the seeded browser lane plan (T0 item 1) | Each migrated spec passes alone, in its file, and under two lane-plan seeds; an invariant ledger row per migrated spec; old and new consumers green |
| S14 | Browser matrix collapse, PR smoke set, `page.clock`, valid media bytes; MCP conformance if 4.2.4 holds. v3 adds: critical-journey inventory at the `gateway-fake` layer (sign-in, submit, progress, cancel, preview, download, upload failure, stalled job, expired artifact, reload and resume), gaps filled; browser ratchets to 0; trace-cost A/B (retain-on-failure vs off, 3 runs each); lane and worker count chosen from per-lane p95 and host load, after all BI2 batches | Reduced matrix still fails on 390 px overflow, a wrong theme token, a revoked key; each journey fails when its control is disabled; ceiling lowered |
| Q | Quarantine tooling (3.3), only when 3.2 triggers it | Q's negative tests |

Per-slice test-second targets from v1 were disk-era figures; they are reset from the S0 A/B.

**T0 contains exactly what later slices depend on:**
1. **Order seed:** `ci-local --order-seed N` (rejected today). A conftest hook shuffles pytest items from the seed, identically on every xdist worker; vitest `--sequence.shuffle --sequence.seed`; the lane planner accepts the seed, but the browser lane plan stays the legacy grouping until the last BI2 batch lands, so no stateful spec meets new neighbors before it is isolated. Exists.
2. **Case receipts:** pytest `--junitxml`, vitest and Playwright JSON merged into `cases.jsonl` (framework, config/project, case ID, outcome, attempt index, duration, seed, SHA). v3: plus `artifacts` (paths) and, for browser cases, lane id and browser version; per-lane setup and test time and a load sample (section 6). Exists; vitest and Playwright write through repository reporters that append one line per attempt, because their JSON reports lose retries (vitest) or are overwritten by a second run.
3. **Signoff receipt** with the refusals in 1.2, and the host lock. v3: scheduled and live runs take the same lock, per task, with gate priority and `LOCK_TIMEOUT` (1.2). Exists (host lock, signoff receipt); the scheduled-run lock and gate priority come with `scripts/test-scheduled`.
4. **`policy`:** retry ban; discovery guard with orphans in `tests/suites.toml`, each with `expires_on` (S13 clears them); skip allowlist (6); no quarantine markers; count ceilings. Self-tests inject each violation. v3: `suites.toml` accepts a `live` tier, so `tests/live/` is run by 1.4 and not an orphan; the browser and fake ratchets of section 6 start at T0's recount. Exists in `scripts/ci/policy.py`; measured at T0 in `tests/suites.toml` and `tests/budgets.toml`.
5. **`scripts/mutate`** (4.5), and green baselines for `category_policy`, `category_enforcement`, `policy` once the `test_route_policy_inventory` baseline failure is explained.
6. **`scripts/test-scheduled`** with the 04:00 timer and owner limits, `SKIPPED`/`INCOMPLETE`, `nightly-red`, per-task locking, 90-day and 20 GiB retention.

**v3 effect on slices in flight:**
- **T0a:** the v3 additions to items 2-4 above are small. If T0a has not merged, add them to it. If it has, they land as T0b, one small PR before BI1. Nothing else in T0a changes.
- **S1:** landed. **S3+P1:** no change.
- **S13:** keep the scope. Moved assertions follow 3.4 rules 1-6, do not raise the T0 ratchets, and carry a layer label. If T0a's ratchets are not yet merged, S13 reports its counts in the PR description instead.

Order: F0 (landed) → T0 → S0 → L1 → S1 (landed) → S13 → S3+P1 → BI1 → BI2a → FK1 → FK2 → S2 → B1 → P2 → G1 → RP1 → RP2 → S4+P3 → S10, S11 → S6, S7 → LV1 → LV2 → S8, S9, S5a, S5b, S15 → LV3 → S12a-c → G2 → BI2b… (batches) → S14 → Q (on demand).

The order is a valid sequence, not the full dependency graph. Hard prerequisites: T0 before everything after it; BI1 before BI2a; BI2a before FK2 and before any change to browser file-to-lane neighbors; FK1 before FK2, S2, RP1 and S6/S7; RP1 before RP2 and LV1; S6/S7 before LV1; S5a/S5b before LV3; all BI2 batches before S14.

Order rationale: F0 removes the one known flake before T0 starts shuffling. T0 precedes S0 so S0 is measured with seeds and case receipts. S0 comes next because the tmpfs change touches every test's runtime and every later timing. S13 stays early because assertions that run nowhere are the largest blind spot. BI1 is cheap and stops the browser hygiene counts from rising while later slices edit specs. BI2a follows it at once, so the isolation fixture and its collision proof exist before FK2 and later slices change browser lanes; the remaining batches are bounded PRs that can interleave. FK1 comes before S2 because S2's loopback fakes should be built once, on the FK core. RP1 follows FK1 and G1 because it reuses both, and it owns the `canaries.toml` schema because the recorder reads it first. LV1 follows S6/S7 because it reruns their conformance cases against real upstreams; LV1 and LV2 cover chat only, and LV3 adds media after S5a/S5b build the media oracles. All BI2 batches precede S14 because S14 may change lanes and workers, which needs isolated data first. Oracle repairs travel with the slice that owns the files.

## 8. Success metrics for the finish report

Same harness before and after (5 runs, random order, load recorded; with 5 runs, report p50 and max, and p95 only from the rolling 20-run window of section 6):

1. Wall time: local loop p50 (gateway, web), gate p50/p95, nightly, weekly; per-lane p50/p95 and setup share, slowest lane.
2. Counts per tier in the units of section 6: collected, deleted (with reason), added, moved.
3. Test-seconds and setup share, against the S0 A/B baseline.
4. Mutation score per core module from a green baseline, equivalents classified. Clean today: `public_failure` 44.3% (70/158), `speech_voices` 85.3%. Not comparable (2 failing baseline tests): `category_policy` 540/659, `category_enforcement` 82.9%, `policy` 238/290. Not measured: five modules.
5. Flakes: first-attempt gate failures per 100 runs, per case and per lane, and separately confirmed flakes (3.1); wall minutes lost to them; nightly suspects; quarantine entries.
6. Blind spots: no-gate files (16 → 0), gate skips (6), `patch.object(proxy…)` (725), `sleep(` (114), browser hygiene ratchets (section 6), ad-hoc fakes (59), protocol × capability cells missing or pending, per source.
7. Selector recall by mutant replay.
8. Live (v3): per night, eligible, executed and skipped cases with skip reasons; `PASS`/`FAIL`/`UPSTREAM_UNAVAILABLE`/`SKIPPED`/`INCOMPLETE`; requests by kind and settled spend against the monthly ceiling; transcript ages against `review_by`.

## 9. Judge resolutions

### 9.1 Judge resolution (v1 review, `audit/dot/judge-design.md`)

| # | Required change | Resolution |
|---|---|---|
| 1 | Preservation ledger | Applied (7): preserved / strengthened / deleted rows; lost kills by mutant ID; mapped renames allowed. |
| 2 | tmpfs premise, disk routing | Applied (4.4): mount table; S0 has its own A/B because the 40 s run moved more than `TMPDIR`. `real_fsync` marker rejected: a process kill cannot observe fsync, and the tmpfs run passed all 7,884. Routing is a `host_disk_dir` fixture. |
| 3 | Quarantine identity, invariants, exit | Applied, simpler (3.2, 3.3): no quarantine until a flake needs it; exact case identity; critical replacements with red/green; 30 recorded passes at one SHA. "3 clean nights" dropped. |
| 4 | T0 prerequisites; lighter slice proof | Applied (7): T0 lists its six parts; `job-notices` split out as F0; one fresh gate + focused red/green per slice. |
| 5 | `public_failure` oracle; exposure vs reachability | Applied (4.3): four behaviors verified on this branch; owner policy first; reachability UNVERIFIED; M1, M3, M4 moved into owning slices. |
| 6 | Merge receipt; revert-to-green | Applied with a limit (1.2, 1.3): signoff refuses unless fresh, complete and on the rechecked tip; GitHub enforcement needs branch protection (owner decision). `nightly-red` blocks signoff. |
| 7 | Measured budgets; clean mutation baseline | Applied (6, 8): provisional targets, time observational, count units, staged ceilings, three dirty baselines marked. |
| 8 | Selector contract; `node_modules` rule | Applied (2, owner rules) as slice L1, since the full gate makes selection non-blocking. |

### 9.2 Judge resolution (v3 review, `audit/dot/judge-v3.md`)

Verdict ACCEPT WITH CHANGES, 15 findings. All 15 applied, F1 with one adaptation (noted). Where the judge offered alternatives (F2, F3), the choice is stated. This was the last review round: v3.1 is final for the overhaul. Later changes follow the slice proof rules in section 7.

| # | Sev. | Finding | Resolution |
|---|---|---|---|
| F1 | HIGH | BI2 namespace collides for two copies of a case; shared owner sessions, global settings, fake state and quotas not isolated | Applied (3.4.7): namespace adds project, worker and an invocation counter, with a mapping in `cases.jsonl`; member principals per mutating test. Adapted: owner-global mutations run in a serial group with snapshot restore (own lane instance where restore is impossible), instead of a principal per test, because owner state cannot be per-principal. FK state keyed by namespace; teardown checks pending jobs, requests and scenario steps; two-copy proof kept (BI2a). |
| F2 | HIGH | Order lacks prerequisites: RP needs LV's config; S6/S7 promise RP cells that do not exist; LV asserts media oracles before S5a/S5b | Applied: the `canaries.toml` schema moves to RP1 (1.4, 4.7); per-source required cells in `tests/matrix.toml` with expiring `pending` cells (4.2.7); LV1/LV2 chat only, LV3 media after S5a/S5b; hard prerequisites listed under the order (7); G-29 corrected (11). |
| F3 | HIGH | FK and BI2 not PR-sized | Applied: FK1 core + one protocol + one consumer, FK2 browser-fake migration, other protocols, edges and scenarios in S2, S4+P3, S5a/S5b, S6/S7 (4.6). BI2a fixture + inventory + pilot, then named batches BI2b…, each leaving old and new consumers green. Also split RP (RP1 machinery, RP2 manual capture) and LV (LV1 runner, LV2 rollout, LV3 media); BI1 splits if spec edits are needed. |
| F4 | HIGH | Two upstream requests per case cannot cover async media | Applied (1.4): per protocol × capability limits for requests, submits, polls, retries, downloads, cancels and wall time in operator data; a lane-owned counting proxy enforces one shared counter at the outbound boundary; health checks count; abandoned jobs are cancelled and reconciled; LV3 tests exhaustion at each step. |
| F5 | HIGH (paid) | Paid ceiling not enforceable | Applied (1.4): one persistent ledger shared by all live runs; calendar-month USD window; prices as dated operator data, unpriced or unbounded cases skipped; worst-case reservation including retries, fallback, polls and downloads; settlement from reported cost or usage, else the reservation stays; idempotent settlement; restart keeps open reservations; bounds this lane only. LV1 tests each rule. |
| F6 | MED | LV oracle overgeneralizes model choice, usage and 5xx | Applied (1.4): cheapest model whose declared features cover the case; required/optional usage per protocol × capability; outcomes classified from both hops, so a gateway-made 5xx or wrong mapping is `FAIL`. |
| F7 | MED | 599 is not a fail-closed oracle; ASGI vs socket evidence | Applied (4.6): per-test violation latch, exact counts and order, required steps consumed, negative test with a gateway that catches the 599; transport faults on loopback only; one clock across gateway and fake, real-clock wiring case kept. |
| F8 | MED | Browser egress enforcement boundary underspecified | Applied (3.4.9): permitted origins named; Chromium host-resolver rules plus context routing with a violation latch; service workers blocked; WebSockets latched; `externalOrigins` for deliberate negative tests; server-side negative test; untested paths UNVERIFIED. |
| F9 | MED | Six-minute target lacks a cost contract; 5-run p95 is noise | Applied (6, 8, 4.8): integer ceiling raises, per-slice cost deltas, virtualized recorded delays, fixture decode cache, fault combinations below the browser; rolling 20-run p50/p95 with sample size and critical path; feasibility UNVERIFIED. |
| F10 | MED | Shared lock can stall the gate; 04:45 may overlap the nightly | Applied (1.2, 1.3, 1.4): per-task locking with gate priority, ≤ 20 min tasks, 30 min `LOCK_TIMEOUT`; live runs triggered by nightly completion; lock wait recorded apart; walls exclude wait, include cleanup; skips never green. |
| F11 | MED | No owner or freshness contract for transcripts and canaries | Applied (1.4, 4.7, 1.3): owner, `review_by` and last observation per canary row and transcript; eligible/executed/skipped totals with reasons; `live-stale` issue after 30 days without `PASS` or past `review_by`, never blocking; 90-day decommission rule; weekly report to a pinned `test-health` issue; transcript size and receipt storage caps. |
| F12 | MED | Lane-log retention needs a runner handshake; B-27 overstated | Applied (3.4.10, B-27): logs in a 0700/0600 receipt run directory, kept by the runner after the lane outcome is final; raw logs restricted, per-case summaries with query strings dropped and secrets masked; only summaries quoted; test for a failed case whose servers exit normally. |
| F13 | MED | "Fail-closed socket guard" overstated | Applied (0, S0, G-1/G-2): described as namespace boundary plus Python diagnostics; S0 adds the loopback-address check and negative tests (same-port remote, UDP, native child, install/restore); runner installation UNVERIFIED until then. |
| F14 | MED | G-23 wrongly says model-pinned eval data breaches AGENTS.md; guard scan too narrow | Applied (G-23, 4.6): decline kept on scope, value and cost; data claim removed; guard scan covers `tests/live/`, recorder and normalizers; registry protocol discriminators allowed. |
| F15 | LOW | Metrics and dispositions overconfident | Applied (3.1, 8, G-8, B-15): first-attempt failures reported apart from confirmed flakes; explicit fixtures adopted, only replacement tooling declined; B-15 rests on the owner's first-result rule. Also took the judge's non-finding notes (manual `test-repeat` equivalent before Q in 3.1; B-22 re-entry on one escaped visual defect). |

## 10. UNVERIFIED

- Every saving before the S0 A/B; the backing of each path inside the sandbox (static reading only).
- Branch protection availability for the `signoff` status.
- The read-only endpoint for "media job in flight".
- The vitest exact-case deselection mechanism (only needed by Q).
- Why `test_route_policy_inventory` failed in the mutation baselines (environment or real).
- Location of `route_method_baseline.json` (seen at `6dc2598`; absent on that branch; present at `tests/fixtures/` on `a97c0ce7`).
- Whether MCP conformance and the SDK corpora pass today.
- Whether a public route passes secret-bearing values into `PublicFailure.details`.
- Scheduled allocations in 1.3; none has been run under the owner's limits.
- v3: whether F0's 20-run proof receipt exists.
- v3: the browser hygiene and fake counts (grep at `a97c0ce7`, not the lint T0 will use); how many `page.route` bodies are inline.
- v3: whether the gateway has a response cache the live lane must bypass, and which upstreams honour a no-cache request.
- v3: which upstreams report billed cost; where none does, settlement uses usage × the configured price (1.4).
- v3: whether eslint-plugin-playwright works with the pinned Playwright 1.62.1 and the repo's ESLint config; the fallback is a `policy` scan.
- v3: the CPU cost of `trace: "retain-on-failure"` on this host (S14 A/B).
- v3: whether any gateway-side media transform needs a perceptual threshold (4.8).
- v3: Schemathesis fit and runtime against our OpenAPI.
- v3: live budgets in 1.4; none has been run.
- v3.1: whether `_hermetic.py` is installed by the runner in every hermetic run (S0 proves it).
- v3.1: Playwright route coverage of redirect hops; the host-resolver rule's coverage of WebRTC, and the equivalent for the nightly Firefox project; any server-side egress boundary for lane gateways (BI1).
- v3.1: whether the 360 s gate target holds with the v3 additions (per-slice deltas, 6).
- v3.1: the number of state-mutating browser spec files and BI2 batch sizes (BI2a inventory).
- v3.1: whether every async media protocol exposes a cancel request the live lane can send.

## 11. Research dispositions (v3)

Source entries: **G** = `testing/gateway-tests-should-default-to-fake-provider…`, **B** = `testing/browser-e2e-suites-stay-non-flaky…`. Both are unverified worker reports.

| # | Recommendation | Disposition |
|---|---|---|
| G-1 | Deterministic default lane: no credentials, egress denied | Adopted. Python exists in two layers: the `--unshare-net` namespace is the boundary, `_hermetic.py` adds connection diagnostics (S0 adds its negative tests). Browser egress guard in BI1 (3.4.9). Gate uses FK/RP only (1.2). |
| G-2 | Unexpected provider traffic fails closed | Adopted. Python: the namespace refuses it. FK: a per-test violation latch fails the test on any unmatched or extra call, even when the gateway absorbs the 599 (4.6). |
| G-3 | Stateful fake provider behind the real adapter boundary | Adopted as FK (4.6): protocol fakes on loopback, registered through the admin API like operator data. |
| G-4 | Fake scenario catalog (LLM, image, job, speech, cross-provider faults) | Adopted (4.6), built per owning slice: FK1, S4+P3, S5a/S5b, S6/S7. |
| G-5 | Virtual time for retries plus one real-timeout wiring case | Adopted (4.4), in S6/S7 and S4. |
| G-6 | Record per wire protocol; replay-only CI; strict matching; keep model and job IDs | Adopted as RP (4.7), replayed through FK. |
| G-7 | VCR.py for Python replay | Declined. It patches the client in process and skips the real transport, and it would be a second mocking tool. FK gives the same replay-only, unmatched-fails behavior over loopback. |
| G-8 | Explicit fixtures/MSW in TypeScript; Polly.js only if capture is needed | Explicit, schema-checked fixtures adopted (G2). MSW and Polly.js declined as replacement tooling: `page.route` is already the browser interception layer (329 calls); vitest keeps its mocks. |
| G-9 | Scrub cassettes; refresh as reviewed maintenance | Adopted (4.7): synthetic request sets, field allowlists, `policy` secret scan, provenance, reviewed refresh PRs. |
| G-10 | Malformed chunk, disconnect and cancellation simulations | Adopted (4.6 catalog), FK and S6/S7. |
| G-11 | Tolerant upstream parsing, strict local normalization; minimal response schemas | Adopted (4 parsing rule; `extra-fields` scenario per protocol). |
| G-12 | Shared conformance suite across adapters; declared capabilities; executed/skipped coverage published | Adopted as the protocol × capability matrix (4.2.7), S6/S7, reported by `policy`. Capabilities come from registry data, not a test list. |
| G-13 | Behavioral invariants: one terminal state, cancellation, finite retries, idempotent callbacks, coherent usage, error mapping | Adopted: P3 (4.3), S4, S6, S10, G1 error envelopes. |
| G-14 | Schema-based property tests at the local gateway, not paid endpoints | Adopted conditionally: Schemathesis weekly from G2 (4.2.6), promoted only by the 4.2.4 rule. |
| G-15 | Structural media checks (decode, MIME, dimensions, duration, rates, channels, non-empty, count) | Adopted (4.8), gate, S5a/S5b. |
| G-16 | Perceptual hash, audio fingerprint, VMAF with calibrated thresholds | Adopted narrowly (4.8): only for deterministic gateway-side media changes, calibrated on good and bad outputs. Nothing built until such a case exists. |
| G-17 | Exact byte hashes only for fixture integrity and deterministic transforms | Adopted (4.6 media bytes, 4.8). |
| G-18 | Human review of changed goldens; keep diagnostic media and diffs on failure | Adopted (4.1, 4.8). |
| G-19 | Live canaries separate, opt-in, budgeted, outside the gate | Adopted as LV (1.4): `nightly-live`, `weekly-live`. |
| G-20 | Live controls: explicit credentials, allowlist, caps, finite retries, hard time limit, spend ceiling including fallback/polling/evaluator, recorded usage and cost, outage vs regression vs skip, stop on budget, no provider switching, rotation | Adopted (1.4): counting proxy with per-capability request limits; monthly spend ledger with worst-case reservations; default ceiling 0 (owner sets it). Allowlist and prices are operator data, not code. |
| G-21 | Cache hits hide current behavior | Adopted (1.4): caches bypassed. Gateway cache existence UNVERIFIED. |
| G-22 | Cheaper models only when they exercise the same contract | Adopted (1.4): the cheapest model whose declared features cover the case's required features. |
| G-23 | Separate quality-evaluation lane (versioned dataset, candidate vs baseline, judge budget) | Declined on scope, value and cost: the gateway does not generate content, and no gateway feature today needs a content-quality oracle (UNVERIFIED for production source). Engine-specific evaluation data would be allowed as operator data under AGENTS.md; that is not the reason. Re-entry: a gateway feature that changes generated content (for example prompt rewriting); then a weekly, budgeted eval lane under 1.4's controls. |
| G-24 | Fixed seeds and low temperature are not reproducibility guarantees | Adopted (1.4): live assertions on contract and shape only. |
| G-25 | Mocked-only unit directory; real-API tests outside CI; slow tests nightly | Adopted as tier placement: hermetic gate; `tests/live/` registered as the `live` tier in `suites.toml` (T0). |
| G-26 | Pin shared conformance packages | Exists (4.2.4, pinned). Schemathesis pinned too. |
| G-27 | Real UI control activation ("a broken button must fail the test") | Adopted (3.4.4); S14 journey proof. |
| G-28 | Browser-to-mocked-gateway and browser-to-real-gateway-to-fake prove different things | Adopted as layer labels (3.4.8), BI1. |
| G-29 | Order: isolation and fakes → contracts and replay → UI flows → media → live and evals | Adopted with changes: isolation (BI2a) and fakes (FK1, FK2) early; contracts and replay (G1, RP, S6/S7) next; live chat (LV1, LV2) before media because it needs only the chat contract; media oracles (S5a/S5b) before live media (LV3); the browser isolation batches and UI flows (BI2b…, S14) last, because they can interleave and S14 depends on them. |
| G-30 | Measure p50/p95 before choosing time budgets | Exists (6: provisional, time observational). Live budgets marked provisional. |
| B-1 | Web-first assertions; no one-shot `isVisible` | Adopted (3.4.1); ratchet from T0/BI1; 0 by S14. |
| B-2 | Roles, labels, test IDs over DOM-shape selectors | Adopted for new and moved code (3.4.6). No bulk rewrite without a measured defect. |
| B-3 | No fixed sleeps; never sleep for generation time | Adopted (3.4.2, 3.4.5): `waitForTimeout` and `networkidle` ratchets; `page.clock` in S14. |
| B-4 | No forced actions; enabled-before-hydration is a readiness defect | Adopted (3.4.3): `force: true` ratchet, fix the app. |
| B-5 | Assert job acceptance, terminal state, preview and download separately | Adopted (3.4.5), S14 journeys. |
| B-6 | One account per parallel worker; namespaced per-test data | Adopted as the `isolated` fixture (3.4.7): a member principal per mutating test, owner-global mutations serialized with restore. One gateway per lane stays (exists). |
| B-7 | Namespace by run, shard, worker and test; explicit reset and seed | Adopted: `<run-id>-<lane>-<project>-<worker>-<invocation>` with a reproduction mapping; fresh lane state directory (exists); snapshot restore for global settings. |
| B-8 | Regenerate expired auth state; keep auth files out of version control | Exists: `global-setup.ts` creates state per run under ignored `test-results/`. |
| B-9 | Browser isolation does not isolate databases, queues or quotas | Adopted (3.4.7): per-principal quotas, namespaced FK state, teardown checks for pending jobs and requests, per-lane state. |
| B-10 | Worker indices collide across shards and runs | Adopted: lane ports 18xxx and the host lock (exist); scheduled and live runs take the lock (1.2). |
| B-11 | Most states on deterministic fixtures; small real-gateway critical-path suite | Adopted (3.4.8): layers; critical journeys at `gateway-fake` (S14). |
| B-12 | Stubbed responses may not match the server; validate fixture schemas | Adopted in G2: fixture JSON validated against `app.openapi()`; inline bodies a ratchet. |
| B-13 | Tiny fixed media and a deterministic fake for job states, slow responses, malformed payloads, reconnects, download failures | Adopted: FK scenarios (4.6), S14 valid media bytes. |
| B-14 | Baseline first: first-attempt failures, p50/p95, setup/test time, slowest shard, saturation | Adopted: T0 `cases.jsonl` plus the v3 per-lane fields; section 8. |
| B-15 | One CI retry for evidence | Declined. Owner rule: the first result stands, so retries are forbidden. A retry could keep first-attempt evidence, but it would still change the recorded result and re-run side effects. Evidence comes from retain-on-failure artifacts and diagnostic repetition (3.1). |
| B-16 | Alert on new first-attempt flakes; triage by affected runs and wasted minutes | Adopted: `nightly-red` (exists in design); weekly per-case failures and lost minutes (3.1, 8). |
| B-17 | Quarantine visible, owned, with deadline; 7-day repair target | Adopted as 3.3 with the owner's numbers: built after 2 days without a fix, 14-day expiry, nightly still runs it. The 7-day target is not adopted; 3.3 already bounds it. |
| B-18 | Tune parallelism only after independence; start conservative; slowest shard bounds speedup | Adopted: no lane, worker or `fullyParallel` change before the last BI2 batch; S14 chooses from per-lane p95 and host load. |
| B-19 | Trace on first retry | Declined: there is no retry. Keep `retain-on-failure` (exists); S14 measures its cost. |
| B-20 | Video retain-on-failure or on first retry | Declined for the gate (recording costs CPU on every test). Available through `scripts/test-repeat`. |
| B-21 | Failure evidence: server logs, job transitions, request and job IDs, browser version, commit | Adopted (3.4.10): BI1 keeps lane logs; T0 adds artifact links and IDs to `cases.jsonl`. |
| B-22 | Narrow visual regression with pinned environment and human review | Declined for now. No baseline infrastructure exists, the UI is still changing often, and S14's semantic checks (overflow, theme token) cover the known visual defects. Re-entry: one escaped visual defect that passes the semantic checks; then a small `toHaveScreenshot` set on fixed media in a pinned container, human-reviewed baselines, no paid service. |
| B-23 | Never compare freshly generated output pixel for pixel | Adopted (3.4.11, 4.8). |
| B-24 | Move combinatorial parameters and API errors below the browser | Exists in v2 (5); extended with a v3 row. |
| B-25 | Risk-based allocation, no fixed e2e/component ratio | Adopted: no ratio target. |
| B-26 | Change frameworks only for concrete gaps | Adopted: stay on Playwright and vitest. |
| B-27 | Restrict artifact access; redact sensitive logs | Adopted (3.4.10): artifacts and lane logs local, 0700/0600, never uploaded; raw logs restricted; per-case summaries drop query strings and mask secrets, and only summaries are quoted. |

## 12. Change log

- **v3.1, 2026-10-12.** Resolved the v3 review (9.2): all 15 findings applied, F1 adapted. Split FK, BI2, RP and LV into PR-sized slices; new order with hard prerequisites; isolation namespace and principals; counting proxy, request limits and spend ledger for live lanes; two-hop live outcomes; FK violation latch and transport rules; browser egress layers; lane-log handshake; lock priority; maintenance and freshness rules; S0 guard tests. Rebased on `main` at `8774d3c3`; S1 marked landed. Final for the overhaul.
- **v3, 2026-10-12.** Applied the two research entries listed in the header; dispositions in section 11. Added: engines-as-data owner rule for fakes; findings rows for egress, scattered fakes, no live coverage, shared browser lane state, browser hygiene counts, lost lane logs (0); live lanes outside the gate with budgets and outcomes, `live-red` not blocking signoff (1.4); scheduled runs take the host lock (1.2); first-attempt and lost-minutes reporting (3.1); browser determinism rules (3.4); tolerant-parse rule, Schemathesis weekly, protocol × capability matrix (4, 4.2.6, 4.2.7); retry virtual clock (4.4); fake upstream (4.6); replay transcripts (4.7); media oracles (4.8); two move rows (5); live and browser and fake budgets and ratchets (6); slices BI1, FK, RP, LV, BI2 and additions to T0, S13, G1, S4+P3, S10, S6/S7, S5a/S5b, G2, S14; new order (7); metrics (8); UNVERIFIED items (10). Marked F0 landed (`a97c0ce7`). Base moved from `chore/fast-gate` to `main` at `a97c0ce7`. Declined: VCR.py, MSW/Polly.js, a quality-eval lane, a CI retry, trace/video on retry, visual regression for now. No v3 review yet (9).
- **v2, 2026-10-12.** Resolved the v1 review (section 9).

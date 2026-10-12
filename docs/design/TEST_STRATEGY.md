# Test strategy

Status: design v3, 2026-10-12. Nothing here is built unless a line says "exists".
Base: `main` at `a97c0ce7` (release 20.1, includes `chore/fast-gate`: pytest-xdist, 10 browser lanes, `scripts/test-affected`).
Inputs: research in `~/code/divan/test-suite-excellence/` and `~/code/divan/testing/`; the audit in `~/Documents/azizam/test-overhaul/audit/` (`consolidated.md`, `sensitive-audit.md`, `measurements.md`, `mutation-raw/`, the `dot/azizam-tso-*` proposals); the v1 review `audit/dot/judge-design.md` (APPROVE WITH CHANGES, resolved in section 9). `audit/mutation.md` does not exist; scores below come from `mutation-raw/`.
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
| **v3.** The hermetic Python tier already denies egress: `bwrap --unshare-net` (`run-hermetic-pytest.sh` 53) and a fail-closed socket guard (`tests/_hermetic.py`). The browser lanes have no egress guard. | Python: keep. Browser: BI1 adds a context-level guard that fails a test on any non-loopback request. |
| **v3.** Upstream fakes are scattered: 59 test files start their own HTTP app or server, 14 use `httpx.MockTransport`; the browser fake is `scripts/ui/fake_engine.py` (166 lines); `tests/compat/harness/fake_backends/` holds a third set. No fake has fault scenarios (split SSE, disconnect, 429). | FK builds one scenario-driven fake upstream per registry protocol. Existing fakes migrate inside their owning slices; the count is a ratchet. |
| **v3.** No test runs against a real upstream on a schedule, and no recorded real-upstream transcript exists. Fakes are written from our idea of the protocol. | RP adds reviewed replay transcripts served by FK. LV adds a budgeted live canary lane outside the gate. |
| **v3.** Browser lanes share one owner and one seed per lane (`global-setup.ts`), with `workers: 1`. Tests in a lane mutate shared server state in sequence. T0's seeded file-to-lane plan will change each test's neighbors. | BI2 adds a per-test namespaced-data fixture. No lane or worker increase before BI2 lands. |
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
- Exclusive host lock (`flock ~/.tmp/azizam-ci.lock`): two gates share 24 cores and ports 18000-18998. Compose still finishes before browser lanes start (`ci-local` 147-152). Scheduled runs (1.3, 1.4) take the same lock.
- The gate uses only deterministic upstreams: FK scenarios and RP transcripts. No credentials, no paid call, no live engine. A test that needs a real upstream belongs to 1.4.
- **Signoff receipt (T0).** `--signoff` is accepted only when all of these hold, else it exits nonzero:
  1. This invocation ran every required step fresh. The tree cache (`ci-local` 50-54) stays for development runs and never yields a signoff.
  2. The receipt records SHA, target branch, target tip, toolchain fingerprint (Python, Node, Chromium, ffmpeg), seed, the required step set and the `policy` result. A partial or interrupted run writes no receipt.
  3. `HEAD^1` equals the recorded target tip, and `git ls-remote` still returns that tip just before `gh signoff create`.
  4. No open `nightly-red` issue exists, unless the PR closes it. `live-red` (1.4) does not block.
- Limit: GitHub does not verify a signoff and the repository has no branch protection (`docs/ops/CI.md`), so "merge only a SHA with a `signoff` status" is a rule, not a lock. Branch protection requiring `signoff` would make it a lock (owner decision; availability UNVERIFIED).

### 1.3 Scheduled tier on the release tip

`scripts/test-scheduled nightly|weekly` (T0). A scheduled pass is never gate evidence.

**Owner decision (reversible, default recorded 2026-10-12):** a systemd user timer runs it on the live machine at 04:00 local, `nice 19` and idle I/O class, at most 8 Python workers and 4 browser lanes, ports 18xxx only. The night is skipped if load average > 24 or a media job is in flight (read-only status query; endpoint UNVERIFIED). A skip is recorded as `SKIPPED`, never as a pass.

Nightly, 60 min, with provisional per-task allocations: web build 6; full Python, 3 seeds, 9; vitest, 5 seeds, 4; browser main config, one seed, 15; full browser matrix + sweep config + Firefox media project (missing Firefox fails) 20; SDK probes on a disposable 18xxx instance over FK upstreams (`tests/compat/node`, `probe_openai.py`) 6. A task that runs out of time writes `INCOMPLETE`, never green. All nightly tasks are deterministic. Real upstreams run only in 1.4.

From BI2 on, the nightly browser main config uses a different lane-plan seed from the gate. A case that fails only with new neighbors is an isolation defect (3.4).

Weekly, 3 h: mutation of the core modules (4.5), selector recall replay (2), borrowed conformance against latest upstream releases (4.2), Schemathesis over the local OpenAPI with FK upstreams (4.2.6, from G2), the RP transcript age report (4.7).

**Red night.** Any failure opens (or updates) a GitHub issue labelled `nightly-red` with seed, case IDs and receipt path. While it is open, signoff refuses (1.2). Diagnose the same day. If no bounded fix exists, revert the change that introduced it. Rerunning until green is not recovery.

Receipts: `~/.tmp/azizam-ci-receipts/` keeps gate and scheduled receipts for 90 days. A PR that cites evidence copies the needed lines into its description, so the evidence outlives the file.

### 1.4 Live lanes (v3, slice LV): opt-in, budgeted, outside the gate

A live result says whether today's real upstream still speaks the contract. It says nothing about a candidate commit, so it never gates and never blocks signoff.

**Scope.** Real upstreams only: the operator's local engines and paid remote providers. The lane runs a disposable 18xxx gateway with the canary services registered through the admin API, as an operator would. It never touches ports 5000/15000 or the live `admin.db`.

**Engines as data.** Committed code is generic: one canary case per protocol × capability, reusing the S6/S7 conformance cases with the upstream switched from FK to the real service. Which services, models and credentials to probe is operator data in a gitignored `canaries.toml` (path in `.env.local`); the repo ships `tests/live/canaries.example.toml`. No engine name appears in lane code.

**Schedule.** `scripts/test-scheduled nightly-live` on its own timer after the nightly (04:45, same `nice`, I/O class, skip rules and host lock); `weekly-live` after the weekly. Separate receipts.

**Budgets (provisional until two weeks of receipts exist):**

| Control | Nightly-live | Weekly-live |
|---|---|---|
| Hard wall | 10 min | 20 min |
| Cases | the core set marked `nightly = true` | core set + a rotation through the rest of `canaries.toml` |
| Concurrency | 1 request | 1 request |
| Requests per case | ≤ 2 (non-stream + stream), counting polling, download and the gateway's own upstream retries | same |
| Output bounds | chat ≤ 32 tokens; image ≤ 256 px side, 1 image; speech/music/SFX ≤ 3 s | + video ≤ 2 s at the lowest listed resolution |
| Paid spend | ceiling `LIVE_SPEND_CEILING_USD`, includes fallback, polling and any evaluator call; default 0, so paid cases are `SKIPPED` until the owner sets it | same ceiling, monthly total |

- Credentials come only from the file passed with `--credentials`; none means paid cases are `SKIPPED`, counted and listed.
- Each case uses the smallest model the service offers for that capability, because the contract is the same.
- Before the call the lane checks the budget left. A case that would exceed it is not started; the lane stops with `INCOMPLETE`. It never switches to another provider to stay inside budget.
- Response caching is bypassed on both sides (gateway cache: existence UNVERIFIED; upstream cache headers where the provider documents them). A cache hit cannot show current behavior.
- Assertions cover contract and shape only: status, envelope, stream event order, usage present and coherent with the gateway receipt, media structure (4.8). Never content. A fixed seed or low temperature is not a reproducibility guarantee.
- The receipt records per case: service id, model, request count, usage, billed cost (from the gateway receipt where the upstream reports it), duration, outcome.

**Outcomes:** `PASS`; `FAIL` (contract broken: wrong shape, missing usage, wrong error mapping); `UPSTREAM_UNAVAILABLE` (connect refused, 5xx, 429, health check failed before the call); `SKIPPED` (no credentials, ceiling 0); `INCOMPLETE` (time or budget out). A `FAIL` opens or updates a `live-red` issue, triaged the same day. The fix is usually a config or transform row (data), sometimes a gateway bug with its own PR and a deterministic regression test in FK or RP. `UPSTREAM_UNAVAILABLE` is listed in the weekly report, not filed, unless it repeats 3 nights in a row.

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
- **Diagnostic repetition is not a retry.** `scripts/test-repeat <case-id> N` (built with Q) writes its own receipt. It never changes a gate or nightly result, and a failed first attempt stays recorded. It may turn on Playwright video and full tracing for the repeated case.
- With zero retries every recorded run is a first attempt, so the first-attempt failure rate is the failure rate in `cases.jsonl`. The weekly report lists, per case, first-attempt failures over the last 100 runs and the wall minutes those failures cost.

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
7. **Isolated data (BI2).** A test that changes server state creates its own entities through the `isolated` fixture. Names and keys carry `<run-id>-<lane>-<case-hash>`. Read-only tests may use the lane seed. Each lane keeps its own gateway, fake upstream, port pair and fresh state directory (exists, `serve.py`). Rate limits and quotas are per entity, so a test never spends another test's quota.
8. **Layer label.** Each spec file declares one layer in `tests/suites.toml`: `intercept` (`page.route` stubs the gateway API; proves the UI's handling of controlled responses), `gateway-fake` (real gateway, FK upstream; proves gateway transport and orchestration too), or `live` (1.4 only). The critical journeys in S14 must exist at the `gateway-fake` layer.
9. **Egress.** Every browser context fails the test on a request to any host other than its lane's loopback ports (BI1).
10. **Evidence.** Gate configs keep `trace: "retain-on-failure"` and `screenshot: "only-on-failure"` (exist; the sweep config has both off). Video stays off in the gate. A failed case's `cases.jsonl` row links its trace, screenshot, the lane's gateway and fake-upstream logs, browser version, SHA and the gateway request and job IDs it saw. Artifacts stay in the receipt directory and are never uploaded.
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
7. **Gate, protocol × capability matrix (v3, S6/S7):** one conformance case set, parametrized by the registry's own `protocol` values and `capabilities` enum (data, read from the registry module, not listed in the test). Each cell either runs or names why it does not apply. `policy` reports executed, skipped and missing cells from `cases.jsonl`, so a capability flag cannot hide a gap. The same cases run against FK (gate), RP transcripts (gate) and real upstreams (1.4).

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
- Loopback ASGI fakes at the HTTP edge (LiteLLM, IdP `/token` + `/jwks`, engine) replace patches of `proxy` internals in the sensitive and route-policy files. From FK on, they live in `tests/support/fake_upstream/` (4.6).
- Clocks and barriers replace outcome-deciding `sleep(` calls; `page.clock` replaces the 75 s and 30 s browser waits. Retry and backoff schedules run on a virtual clock; each retrying path keeps one case with a real, short timeout to prove the timeout is wired.
- `tests/support/` replaces the cross-test imports of `test_admin_oidc`, `test_exposure_oidc_security`, `tests.test_admin_api`.

### 4.5 Mutation testing

- `scripts/mutate <module>` (T0, from the audit harness) refuses to score unless its baseline run is green. It writes each mutant's ID, status and killer case IDs.
- Core modules: `category_policy`, `category_enforcement`, `policy`, `public_failure`, `speech_voices`, `transforms`, `media_jobs`, `receipt_store`, `admin_store` (auth paths), `runtime_auth`.
- Survivors are classified (killable, equivalent, unproductive) in a repo file. Score = killed ÷ (all − equivalent).
- Uses: named mutants for the slice ledger (section 7); the weekly report; final milestones. Never a gate.

### 4.6 Fake upstream (v3, slice FK)

One package, `tests/support/fake_upstream/`, serves every deterministic upstream the tests need.

- **One app per wire protocol** the registry accepts (grep finds at least `openai-compatible`, `anthropic`, `ollama` and `comfyui` in `gateway/`; FK reads the full list from the registry, UNVERIFIED here), plus the non-provider edges S2 needs (LiteLLM admin, IdP `/token` + `/jwks`). It grows a protocol only when the registry does.
- **Scenarios are data.** A scenario is a JSON file: request matcher, ordered response steps (status, headers, body or SSE chunks, delays on the virtual clock, disconnect). Code branches on protocol and step type only. The engine-knowledge guard's scan is extended to this package: an engine name in a branch fails, as in `gateway/`.
- **Engine quirks.** A test for an engine quirk is a scenario row that reproduces the quirk plus the transform config row (`config["transforms"]`) an operator would add. No fake subclass per engine.
- **Registration.** Pytest wires the fake through `httpx.ASGITransport` or a loopback port; browser lanes run it as the lane's upstream process and register it through the admin API, as `global-setup.ts` already does for `capture-engine`. FK replaces `scripts/ui/fake_engine.py` behind the same seed rows.
- **Fail closed.** A request that matches no scenario step gets HTTP 599 with the unmatched request in the body, and the test fails on it. The fake records every request; tests assert the forwarded request (model, parameters, job ID), not only the response.
- **Scenario catalog,** built when a slice needs it, not all in FK:
  - LLM: text, structured output, refusal, tool calls, empty output, truncated JSON, SSE event split across chunks, malformed chunk, disconnect mid-stream, `extra-fields`.
  - Image: bytes, artifact URL, wrong MIME, wrong dimensions, corrupt download, expired URL.
  - Jobs (video, music, image queues): queued → running → completed, failed, slow polling, cancel during run, duplicate completion callback.
  - Speech: valid audio, silent audio, wrong sample rate, partial stream.
  - Every protocol: 401, 429 with `Retry-After`, 5xx, connect timeout, read timeout, fallback to the next backend, usage reporting, bounded retries.
- **Fake drift.** A fake shows only what we believe the protocol is. RP transcripts (4.7) and live canaries (1.4) check that belief. A scenario copied from a transcript names it in its provenance field.
- **Media bytes.** Fixed tiny files (exists in part: `tests/media_test_bytes.py`) with recorded MIME, dimensions, duration, sample rate and SHA-256.
- **Migration.** FK moves `scripts/ui/fake_engine.py`, `tests/compat/harness/fake_backends/` and the fakes of the files it touches. Other ad-hoc fakes (59 files) move inside their owning slices. The count is a ratchet.

### 4.7 Replay transcripts (v3, slice RP)

A transcript is a recorded exchange with a real upstream, replayed by FK as a scenario. It catches shapes the fake author did not imagine. It cannot show the upstream still behaves that way; 1.4 does that.

- **Recording** is by hand only: `scripts/record-upstream --service <id from canaries.toml> --set <request set>`, run by the owner against the upstream directly, never through ports 5000/15000. The request set is committed and synthetic, so no real prompt or upload is recorded.
- **Kept data.** An allowlist of fields and headers per protocol; everything else is dropped. Hosts and signed URLs are replaced by path-named normalizers (as in 4.1). Binary media is stored apart from the metadata, under the media-bytes rules of 4.6.
- **Scan before commit.** `policy` scans transcripts and fixtures for key prefixes (`opk_`, `oak_`, `sk-`), bearer tokens, home paths and LAN addresses, and fails on a hit. Scrubbing is incomplete by nature (`testing/record-replay-test-tools-treat-cassette-scrubbing…`), so the allowlist and the synthetic request set carry the load and the scan is a backstop.
- **Provenance** per transcript: service protocol, engine and version as reported by the upstream (data), date, request-set hash, recorder version.
- **Replay** is strict: method, path, allowlisted headers and the normalized semantic body must match, including model, parameters and job IDs. A miss fails (4.6). There is no passthrough mode.
- **Staleness.** The weekly report lists each transcript's age and whether the last live canary for that service passed. Refresh is a reviewed PR with the diff of the normalized transcript. Nothing refreshes automatically.
- **Tool.** Not VCR.py and not Polly.js (section 11, G-7, G-8).

### 4.8 Media oracles (v3, S5a/S5b)

- **Gate, every media result:** decodes; MIME; pixel dimensions, or duration, sample rate, channels and frame rate; not empty and not silent where content is expected; artifact count. Decoding uses the pinned toolchain in the receipt fingerprint (ffmpeg).
- **Exact SHA-256** only for fixture integrity and deterministic gateway-side transforms (for example a pass-through or a fixed re-encode with pinned tools).
- **Perceptual distance** (dHash/pHash, audio fingerprint, VMAF) only where the gateway itself changes media deterministically and a byte hash is too strict. A threshold is set from measured good and deliberately bad outputs and recorded with them. Today no such case is known (UNVERIFIED), so none is built in advance.
- **Never** a perceptual or content check on generated output in the gate or in 1.4.
- On failure the harness stores the actual media and a diff image or report in the receipt directory.

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
| `scripts/ui/fake_engine.py`, `tests/compat/harness/fake_backends/` (v3) | Move into `tests/support/fake_upstream/` (FK) | Three fakes of one protocol drift apart. |
| Browser cases that vary provider, model or parameter combinations or API errors (v3) | Move to API or vitest tests unless the UI shows a distinct state | Combinations belong below the browser. |

Not merged (different seams): the redaction layers, swap drain unit vs lifecycle, the prompt-memory family, origin-bound secret layers.

## 6. Budgets

Every target is provisional until measured on this host at the recorded load. Time is observational: the weekly report shows the median of the last 5 gate runs; no gate fails on time.

**Count units:** pytest collected items (each parameter set counts; subtests reported, not counted); vitest test cases; Playwright test × project. Quarantined cases count before deselection, so quarantine cannot buy budget.

**Count ceilings** in `tests/budgets.toml`, enforced by `policy` from T0. T0 sets them at today's counts. A slice that must add cases (S13 adds ≤ 40 browser cases; FK and S6/S7 add the protocol × capability matrix) raises the ceiling in the same PR with a reason; S14 lowers it. No other raises.

| Tier | Today (measured) | Provisional target |
|---|---|---|
| Python gate | 7,891 collected + 2,803 subtests; 287 s wall (229-325); 2,430 test-s | ≤ 8,000 collected, plus the FK/S6/S7 matrix raise; wall and test-s set from the S0 A/B |
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

**Extra for slices that change shared fixtures, isolation or runner state (S0, `tests/support/`, FK, BI2, Q) and for milestones:** three full random-order runs with recorded seeds, and the mutation diff over all core modules.

| # | Slice | Extra proof |
|---|---|---|
| F0 | `job-notices.spec.tsx` flake fix. Landed in `a97c0ce7`. | 0 failures in 20 shuffled runs under load (receipt not checked by this revision) |
| T0 | Prerequisites (below). In flight as T0a. | Each `policy` self-test fails on injection; signoff refuses in each case of 1.2; two seeds give identical item order on every xdist worker |
| S0 | `TMPDIR` on tmpfs, `host_disk_dir` fixture | The A/B in 4.4 |
| L1 | Selector contract (section 2) | Exit 2 on a per-file orphan; `--changed-path` replay matches git-diff selection |
| S1 | DD1-DD21, DA1-DA9. In flight; unchanged by v3. | Ledger rows from `consolidated.md` 2.1 |
| S13 | Orphaned browser files. In flight; v3 adds: moved code follows 3.4 rules 1-6 and must not raise the browser ratchets; each moved spec gets its layer label. | Per moved assertion, a UI mutation fails the gated spec; ceiling raise ≤ 40 |
| S3+P1 | Enforcement, route policy; P1 properties. In flight; unchanged by v3. | S3 mutants; `_report` fails on 400/422/500 |
| BI1 (v3) | Browser guards: egress guard per context (3.4.9); layer labels in `suites.toml` for every spec; eslint-plugin-playwright (pinned) or a `policy` scan for the five hygiene patterns, as ratchets; `serve.py` keeps lane logs of failed runs (0600) and the case row links them | An injected external request fails its test; an added `waitForTimeout`, `force: true` or one-shot assertion fails `policy`; a failing case's row names a log file that exists |
| FK (v3) | Fake upstream (4.6): package, scenario format, fail-closed matcher, request log, guard scan extension; moves `scripts/ui/fake_engine.py` and `tests/compat/harness/fake_backends/`; the cross-protocol fault scenarios | Browser lanes pass on FK with unchanged seed rows; an engine-name branch in the package fails the guard; an unmatched request fails the test; each fault scenario has a case that fails when the gateway's handling of it is reverted |
| S2 | Admin plane on the real store (M1, M4); sensitive rewrites over loopback fakes, now FK edges (LiteLLM, IdP) | Audit-paging and credential mutants; revoked key gives 401 through the loopback |
| B1 | Schema provenance, M3 fix | A renamed serializer field fails the schema check |
| P2 | `public_failure`, `transforms`, `speech_voices` | The four characterizations in 4.3; owner policy recorded |
| G1 | Golden harness: catalogs, discovery, error envelopes; failure diffs into the receipt directory | An added field, a reordered list and a leaked canary each fail |
| RP (v3) | Replay transcripts (4.7): `scripts/record-upstream`, synthetic request sets, allowlists, provenance, the secret scan in `policy`, first transcripts for the OpenAI-compatible chat protocol | A planted key, home path or LAN address fails `policy`; a changed model or job ID in the request fails replay; a transcript-only shape (one the hand-written fake lacked) is covered by a case |
| S4+P3 | Prompt memory, job recovery; state machines, including duplicate callback and cancellation (4.3) | S4 mutants; barriers replace sleeps |
| S10, S11 | Receipts and spend, including usage coherence between upstream usage and the receipt; Python renderers | S10/S11 mutants |
| S6, S7 | Execution, providers; conformance, compat; SDK differential clients; the protocol × capability matrix (4.2.7) over FK and RP; virtual-clock retry schedules plus one real-timeout case per retrying path | Forwarder-removed and DONE-ignored mutants fail, not skip; a removed capability cell shows as missing in `policy` |
| LV (v3) | Live lanes (1.4): `tests/live/`, `canaries.example.toml`, `nightly-live` and `weekly-live` tasks and timers, budget accounting, outcomes, `live-red` | With credentials absent, paid cases are `SKIPPED` and counted; a ceiling below one case's cost stops the lane `INCOMPLETE` before any call; a stopped upstream gives `UPSTREAM_UNAVAILABLE`, not `FAIL`; a fault scenario served as a "live" upstream gives `FAIL`; no gate step imports `tests/live/` |
| S8, S9, S5a, S5b, S15 | Lifecycle, manifests, media (structural oracles of 4.8 on decoded pixels/PCM), platform | Per-slice mutants in `consolidated.md`; a wrong-dimension and a silent-audio FK scenario each fail |
| S12a-c | Vitest oracle rewrites | Classifier and seek mutants |
| G2 | Full OpenAPI, protocol transcripts, receipt projections; intercepted JSON in `web/e2e/fixtures/` validated against `app.openapi()`, inline `page.route` bodies as a ratchet; Schemathesis weekly (4.2.6) | As G1; a fixture with a removed required field fails |
| BI2 (v3) | `isolated` fixture (3.4.7); state-mutating specs move to it; hygiene fixes in the files it touches; nightly runs a second lane-plan seed (1.3) | Each migrated spec passes alone, in its file, and under two lane-plan seeds; two copies of one migrated spec run in parallel in one lane without interference |
| S14 | Browser matrix collapse, PR smoke set, `page.clock`, valid media bytes; MCP conformance if 4.2.4 holds. v3 adds: critical-journey inventory at the `gateway-fake` layer (sign-in, submit, progress, cancel, preview, download, upload failure, stalled job, expired artifact, reload and resume), gaps filled; browser ratchets to 0; trace-cost A/B (retain-on-failure vs off, 3 runs each); lane and worker count chosen from per-lane p95 and host load, after BI2 only | Reduced matrix still fails on 390 px overflow, a wrong theme token, a revoked key; each journey fails when its control is disabled; ceiling lowered |
| Q | Quarantine tooling (3.3), only when 3.2 triggers it | Q's negative tests |

Per-slice test-second targets from v1 were disk-era figures; they are reset from the S0 A/B.

**T0 contains exactly what later slices depend on:**
1. **Order seed:** `ci-local --order-seed N` (rejected today). A conftest hook shuffles pytest items from the seed, identically on every xdist worker; vitest `--sequence.shuffle --sequence.seed`; the lane planner seeds file-to-lane assignment.
2. **Case receipts:** pytest `--junitxml`, vitest and Playwright JSON merged into `cases.jsonl` (framework, config/project, case ID, outcome, attempt index, duration, seed, SHA). v3: plus `artifacts` (paths) and, for browser cases, lane id and browser version; per-lane setup and test time and a load sample (section 6).
3. **Signoff receipt** with the refusals in 1.2, and the host lock. v3: scheduled and live runs take the same lock.
4. **`policy`:** retry ban; discovery guard with orphans in `tests/suites.toml`, each with `expires_on` (S13 clears them); skip allowlist (6); no quarantine markers; count ceilings. Self-tests inject each violation. v3: `suites.toml` accepts a `live` tier, so `tests/live/` is run by 1.4 and not an orphan; the browser and fake ratchets of section 6 start at T0's recount.
5. **`scripts/mutate`** (4.5), and green baselines for `category_policy`, `category_enforcement`, `policy` once the `test_route_policy_inventory` baseline failure is explained.
6. **`scripts/test-scheduled`** with the 04:00 timer and owner limits, `SKIPPED`/`INCOMPLETE`, `nightly-red`, 90-day retention.

**v3 effect on slices in flight:**
- **T0a:** the v3 additions to items 2-4 above are small. If T0a has not merged, add them to it. If it has, they land as T0b, one small PR before BI1. Nothing else in T0a changes.
- **S1, S3+P1:** no change.
- **S13:** keep the scope. Moved assertions follow 3.4 rules 1-6, do not raise the T0 ratchets, and carry a layer label. If T0a's ratchets are not yet merged, S13 reports its counts in the PR description instead.

Order: F0 (landed) → T0 → S0 → L1 → S1 → S13 → S3+P1 → BI1 → FK → S2 → B1 → P2 → G1 → RP → S4+P3 → S10, S11 → S6, S7 → LV → S8, S9, S5a, S5b, S15 → S12a-c → G2 → BI2 → S14 → Q (on demand).

Order rationale: F0 removes the one known flake before T0 starts shuffling. T0 precedes S0 so S0 is measured with seeds and case receipts. S0 comes next because the tmpfs change touches every test's runtime and every later timing. S13 stays early because assertions that run nowhere are the largest blind spot. BI1 is cheap and stops the browser hygiene counts from rising while later slices edit specs. FK comes before S2 because S2's loopback fakes should be built once, in FK. RP follows FK and G1 because it reuses both. LV follows S6/S7 because it reruns their conformance cases against real upstreams. BI2 precedes S14 because S14 may change lanes and workers, which needs isolated data first. Oracle repairs travel with the slice that owns the files.

## 8. Success metrics for the finish report

Same harness before and after (5 runs, random order, load recorded):

1. Wall time: local loop p50 (gateway, web), gate p50/p95, nightly, weekly; per-lane p50/p95 and setup share, slowest lane.
2. Counts per tier in the units of section 6: collected, deleted (with reason), added, moved.
3. Test-seconds and setup share, against the S0 A/B baseline.
4. Mutation score per core module from a green baseline, equivalents classified. Clean today: `public_failure` 44.3% (70/158), `speech_voices` 85.3%. Not comparable (2 failing baseline tests): `category_policy` 540/659, `category_enforcement` 82.9%, `policy` 238/290. Not measured: five modules.
5. Flakes: first-attempt gate failures per 100 runs, per case and per lane; wall minutes lost to them; nightly suspects; quarantine entries.
6. Blind spots: no-gate files (16 → 0), gate skips (6), `patch.object(proxy…)` (725), `sleep(` (114), browser hygiene ratchets (section 6), ad-hoc fakes (59), protocol × capability cells missing.
7. Selector recall by mutant replay.
8. Live (v3): per night, cases `PASS`/`FAIL`/`UPSTREAM_UNAVAILABLE`/`SKIPPED`/`INCOMPLETE`, requests and billed cost against the ceiling; transcript ages.

## 9. Judge resolution (v1 review, `audit/dot/judge-design.md`)

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

v3 has not had an independent review. It should get one before BI1, FK, RP or LV start.

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
- v3: which upstreams report billed cost; where none does, the live receipt records usage only and the ceiling uses a configured price per unit.
- v3: whether eslint-plugin-playwright works with the pinned Playwright 1.62.1 and the repo's ESLint config; the fallback is a `policy` scan.
- v3: the CPU cost of `trace: "retain-on-failure"` on this host (S14 A/B).
- v3: whether any gateway-side media transform needs a perceptual threshold (4.8).
- v3: Schemathesis fit and runtime against our OpenAPI.
- v3: live budgets in 1.4; none has been run.

## 11. Research dispositions (v3)

Source entries: **G** = `testing/gateway-tests-should-default-to-fake-provider…`, **B** = `testing/browser-e2e-suites-stay-non-flaky…`. Both are unverified worker reports.

| # | Recommendation | Disposition |
|---|---|---|
| G-1 | Deterministic default lane: no credentials, egress denied | Adopted. Exists for Python (`--unshare-net`, `_hermetic.py`). Browser egress guard in BI1 (3.4.9). Gate uses FK/RP only (1.2). |
| G-2 | Unexpected provider traffic fails closed | Adopted. Python exists; FK returns 599 and fails the test on any unmatched request (4.6). |
| G-3 | Stateful fake provider behind the real adapter boundary | Adopted as FK (4.6): protocol fakes on loopback, registered through the admin API like operator data. |
| G-4 | Fake scenario catalog (LLM, image, job, speech, cross-provider faults) | Adopted (4.6), built per owning slice: FK, S4+P3, S5a/S5b, S6/S7. |
| G-5 | Virtual time for retries plus one real-timeout wiring case | Adopted (4.4), in S6/S7 and S4. |
| G-6 | Record per wire protocol; replay-only CI; strict matching; keep model and job IDs | Adopted as RP (4.7), replayed through FK. |
| G-7 | VCR.py for Python replay | Declined. It patches the client in process and skips the real transport, and it would be a second mocking tool. FK gives the same replay-only, unmatched-fails behavior over loopback. |
| G-8 | Explicit fixtures/MSW in TypeScript; Polly.js only if capture is needed | Declined. `page.route` is already the browser interception layer (329 calls); vitest keeps its mocks. Fixture validity is handled in G2. |
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
| G-20 | Live controls: explicit credentials, allowlist, caps, finite retries, hard time limit, spend ceiling including fallback/polling/evaluator, recorded usage and cost, outage vs regression vs skip, stop on budget, no provider switching, rotation | Adopted (1.4). Paid ceiling defaults to 0 (owner sets it). Allowlist is operator data, not code. |
| G-21 | Cache hits hide current behavior | Adopted (1.4): caches bypassed. Gateway cache existence UNVERIFIED. |
| G-22 | Cheaper models only when they exercise the same contract | Adopted (1.4): smallest model per protocol × capability. |
| G-23 | Separate quality-evaluation lane (versioned dataset, candidate vs baseline, judge budget) | Declined. The gateway does not generate content; quality belongs to the operator's engines, and a model-pinned eval set is engine tracking in tests, against AGENTS.md. Re-entry: a gateway feature that changes generated content (for example prompt rewriting); then a weekly, budgeted eval lane under 1.4's controls. |
| G-24 | Fixed seeds and low temperature are not reproducibility guarantees | Adopted (1.4): live assertions on contract and shape only. |
| G-25 | Mocked-only unit directory; real-API tests outside CI; slow tests nightly | Adopted as tier placement: hermetic gate; `tests/live/` registered as the `live` tier in `suites.toml` (T0). |
| G-26 | Pin shared conformance packages | Exists (4.2.4, pinned). Schemathesis pinned too. |
| G-27 | Real UI control activation ("a broken button must fail the test") | Adopted (3.4.4); S14 journey proof. |
| G-28 | Browser-to-mocked-gateway and browser-to-real-gateway-to-fake prove different things | Adopted as layer labels (3.4.8), BI1. |
| G-29 | Order: isolation and fakes → contracts and replay → UI flows → media → live and evals | Adopted with one change: media structural checks (S5a/S5b) stay before the browser work (BI2, S14) because they are already planned there. |
| G-30 | Measure p50/p95 before choosing time budgets | Exists (6: provisional, time observational). Live budgets marked provisional. |
| B-1 | Web-first assertions; no one-shot `isVisible` | Adopted (3.4.1); ratchet from T0/BI1; 0 by S14. |
| B-2 | Roles, labels, test IDs over DOM-shape selectors | Adopted for new and moved code (3.4.6). No bulk rewrite without a measured defect. |
| B-3 | No fixed sleeps; never sleep for generation time | Adopted (3.4.2, 3.4.5): `waitForTimeout` and `networkidle` ratchets; `page.clock` in S14. |
| B-4 | No forced actions; enabled-before-hydration is a readiness defect | Adopted (3.4.3): `force: true` ratchet, fix the app. |
| B-5 | Assert job acceptance, terminal state, preview and download separately | Adopted (3.4.5), S14 journeys. |
| B-6 | One account per parallel worker; namespaced per-test data | Adopted as the `isolated` fixture (3.4.7), BI2. One gateway per lane stays (exists). |
| B-7 | Namespace by run, shard, worker and test; explicit reset and seed | Adopted: `<run-id>-<lane>-<case-hash>` names; fresh lane state directory (exists). |
| B-8 | Regenerate expired auth state; keep auth files out of version control | Exists: `global-setup.ts` creates state per run under ignored `test-results/`. |
| B-9 | Browser isolation does not isolate databases, queues or quotas | Adopted (3.4.7): per-entity quotas; per-lane state. |
| B-10 | Worker indices collide across shards and runs | Adopted: lane ports 18xxx and the host lock (exist); scheduled and live runs take the lock (1.2). |
| B-11 | Most states on deterministic fixtures; small real-gateway critical-path suite | Adopted (3.4.8): layers; critical journeys at `gateway-fake` (S14). |
| B-12 | Stubbed responses may not match the server; validate fixture schemas | Adopted in G2: fixture JSON validated against `app.openapi()`; inline bodies a ratchet. |
| B-13 | Tiny fixed media and a deterministic fake for job states, slow responses, malformed payloads, reconnects, download failures | Adopted: FK scenarios (4.6), S14 valid media bytes. |
| B-14 | Baseline first: first-attempt failures, p50/p95, setup/test time, slowest shard, saturation | Adopted: T0 `cases.jsonl` plus the v3 per-lane fields; section 8. |
| B-15 | One CI retry for evidence | Declined. Owner rule: zero retries. A retry hides first-attempt evidence in the result and re-runs side effects. Evidence comes from retain-on-failure artifacts and `scripts/test-repeat`. |
| B-16 | Alert on new first-attempt flakes; triage by affected runs and wasted minutes | Adopted: `nightly-red` (exists in design); weekly per-case failures and lost minutes (3.1, 8). |
| B-17 | Quarantine visible, owned, with deadline; 7-day repair target | Adopted as 3.3 with the owner's numbers: built after 2 days without a fix, 14-day expiry, nightly still runs it. The 7-day target is not adopted; 3.3 already bounds it. |
| B-18 | Tune parallelism only after independence; start conservative; slowest shard bounds speedup | Adopted: no lane, worker or `fullyParallel` change before BI2; S14 chooses from per-lane p95 and host load. |
| B-19 | Trace on first retry | Declined: there is no retry. Keep `retain-on-failure` (exists); S14 measures its cost. |
| B-20 | Video retain-on-failure or on first retry | Declined for the gate (recording costs CPU on every test). Available through `scripts/test-repeat`. |
| B-21 | Failure evidence: server logs, job transitions, request and job IDs, browser version, commit | Adopted (3.4.10): BI1 keeps lane logs; T0 adds artifact links and IDs to `cases.jsonl`. |
| B-22 | Narrow visual regression with pinned environment and human review | Declined for now. No baseline infrastructure exists, the UI is still changing often, and S14's semantic checks (overflow, theme token) cover the known visual defects. Re-entry: two visual defects that pass the semantic checks; then a small `toHaveScreenshot` set on fixed media in a pinned container, human-reviewed baselines, no paid service. |
| B-23 | Never compare freshly generated output pixel for pixel | Adopted (3.4.11, 4.8). |
| B-24 | Move combinatorial parameters and API errors below the browser | Exists in v2 (5); extended with a v3 row. |
| B-25 | Risk-based allocation, no fixed e2e/component ratio | Adopted: no ratio target. |
| B-26 | Change frameworks only for concrete gaps | Adopted: stay on Playwright and vitest. |
| B-27 | Restrict artifact access; redact sensitive logs | Adopted: artifacts and lane logs local, 0600, never uploaded; live receipts drop URL query strings (1.4, 3.4.10). |

## 12. Change log

- **v3, 2026-10-12.** Applied the two research entries listed in the header; dispositions in section 11. Added: engines-as-data owner rule for fakes; findings rows for egress, scattered fakes, no live coverage, shared browser lane state, browser hygiene counts, lost lane logs (0); live lanes outside the gate with budgets and outcomes, `live-red` not blocking signoff (1.4); scheduled runs take the host lock (1.2); first-attempt and lost-minutes reporting (3.1); browser determinism rules (3.4); tolerant-parse rule, Schemathesis weekly, protocol × capability matrix (4, 4.2.6, 4.2.7); retry virtual clock (4.4); fake upstream (4.6); replay transcripts (4.7); media oracles (4.8); two move rows (5); live and browser and fake budgets and ratchets (6); slices BI1, FK, RP, LV, BI2 and additions to T0, S13, G1, S4+P3, S10, S6/S7, S5a/S5b, G2, S14; new order (7); metrics (8); UNVERIFIED items (10). Marked F0 landed (`a97c0ce7`). Base moved from `chore/fast-gate` to `main` at `a97c0ce7`. Declined: VCR.py, MSW/Polly.js, a quality-eval lane, a CI retry, trace/video on retry, visual regression for now. No v3 review yet (9).
- **v2, 2026-10-12.** Resolved the v1 review (section 9).

# Test strategy

Status: design, 2026-10-12. Nothing here is built unless a line says "exists".
Base: `chore/fast-gate` (release 18 + pytest-xdist, 10 planned browser lanes, `scripts/test-affected`).
Inputs: research in `~/code/divan/test-suite-excellence/` and `~/code/divan/testing/`; the audit in `~/Documents/azizam/test-overhaul/audit/` (`consolidated.md`, `sensitive-audit.md`, `raw/`, `mutation-raw/`, the six `dot/azizam-tso-*` proposals). `audit/measurements.md` and `audit/mutation.md` did not exist yet; figures that depend on them are UNVERIFIED.

Owner rules, applied everywhere: no automatic retries; no coverage gate; count and time are budgets; a rewritten test must fail on a revert or mutant, then pass; every deletion states its reason.

## 0. Findings and decisions

| Finding | Decision |
|---|---|
| Strong suites gate the integrated candidate fast and run slower tiers on a schedule (converge, patterns 1-2). | Three tiers. The PR gate is the **full** hermetic gate on the exact merge candidate. |
| Affected-only selection is safe only with a full backstop on the merge candidate (Shopify 99.94% recall, with full runs kept). | Selection serves the local loop only. At 7-8 min the full gate is cheaper than selector risk in the gate. |
| Kubernetes banned retries and quarantines flakes to owners (green 42% → 90%+). | Zero retries. One quarantine file with owner, issue and expiry, validated by the gate. |
| Mature suites differ mostly in oracle choice. | Every area names its oracle (section 4). |
| 86% KEEP, 0.4% DELETE; 702 REWRITE. | The work is oracle quality and fixture cost, not deletion. |
| 35% of 2,430 test-seconds is fixture setup; fsync costs ~71 ms per commit on the disk state root. | S0 (tmpfs base temp, template databases) before per-file rewrites. |
| 19 files run in no gate (17 `*.e2e.ts`, `app-shell.playwright.spec.ts`, `services-route.spec.mjs`); 10 area Playwright configs under `web/src` are wired to nothing. | A discovery guard fails on any test file no tier runs. |
| `category_policy` mutation: 540/659 killed, but its baseline had 2 failing tests and 46 kills came from them. `public_failure`: 70/158. | Mutation scores count only from a green baseline. They are slice evidence and a scheduled report, never a global gate. |
| Coverage gates are rare and contested. | No coverage threshold. |
| 85 static FLAKY labels had 0 failures in 5 pytest runs; an unlabelled vitest test (`job-notices.spec.tsx`) failed 1 in 5. | Quarantine nothing on labels. Fix `job-notices` in T0. |

## 1. Tiers

### 1.1 Local loop

```bash
scripts/test-affected --list          # show the selection
scripts/test-affected -- -n 4         # affected Python (hermetic, xdist) + vitest --changed
scripts/test-affected --e2e -- -n 4   # + Playwright --only-changed (build web/ first)
```

- Change: lanes use 4 workers, not 12. Many lanes share one 24-core host.
- Budget: p50 ≤ 90 s for a gateway change, ≤ 30 s web-only. Today: UNVERIFIED (a gateway change selects 145-231 of 324 Python modules because most reach `proxy.py`).
- A local pass is never merge evidence.

### 1.2 PR gate, on the merge candidate

```bash
scripts/ci-local --force --signoff    # at the merge candidate commit
```

- Merge candidate: the target tip with the PR merged, i.e. the exact commit that becomes the new tip. If the target moves, run again.
- Runs everything it runs today: web check, vitest, hermetic pytest (`-n 12 --dist worksteal`), fresh native and Compose journeys, 10 main browser lanes, live-data, household. New steps from T0: `policy` (section 3.5) and `budget` (section 6).
- New: an exclusive host lock (`flock ~/.tmp/azizam-ci.lock`). Two gates share 24 cores and ports 18000-18998; the second waits. The dot's "2 worker slots" budget is rejected: the measured layout (12 workers + 10 lanes) already fits.
- Cache key becomes git tree + toolchain fingerprint (Python, Node, Chromium, ffmpeg). `policy` always reruns, because quarantine expiry depends on the date.
- Budget: p50 ≤ 6 min, ceiling 8 min. Today 421-488 s.

### 1.3 Scheduled tier, on the release tip

`scripts/test-scheduled nightly|weekly` (new). Results go to `~/.tmp/azizam-ci-receipts/scheduled/<date>/` (`summary.json` + Markdown). A scheduled pass is never gate evidence.

Nightly, ≤ 60 min:
1. Full gate in random order, `ci-local --force --order-seed $SEED` (new flag): a conftest hook shuffles pytest items, vitest uses `--sequence.shuffle --sequence.seed`, and the lane planner shuffles file-to-lane assignment.
2. Flake detection: pytest 3 more seeds, vitest 5 seeds, each quarantined case 10 times.
3. Full browser matrix (every route × 2 themes × 390/768/1440 px), the sweep config, the Firefox media project (a missing Firefox fails the run instead of shrinking it).
4. Borrowed conformance against the latest upstream releases (section 4.2). Non-blocking; it reports drift before a pin moves.
5. Live probes (`tests/compat/node`, the remote probes in `probe_openai.py`) against a disposable instance on 18xxx ports. Never 5000 or 15000.
6. Diff-aware mutation of core modules changed since the last night.

Weekly, ≤ 3 h: full mutation of the core modules; selector recall replay (2.2).

## 2. Affected-only selection

- `scripts/test-affected` (exists) stays the local selector and never feeds the gate. The dot's `test-plan.py`/`test-gate` design is rejected: it would rebuild `ci-local` to save minutes the gate no longer costs.
- Whole-suite triggers (exist): conftest, `_hermetic.py`, lock files, `requirements.txt`, `proxy_config.yml`, `pytest.ini`, `pyproject.toml`, the runner. Add: `tests/support/**`, fixture files no test names by path, `tests/quarantine.json`, `tests/budgets.toml`, `scripts/test-affected` itself. For the web: `web/e2e/fixtures/**`, `global-setup.ts` and any Playwright config select every browser spec.
- An empty selection for a changed production file exits 2 with "no test reaches X". It is not a pass.

**Recall** (weekly report):
1. *Mutant replay, primary and deterministic.* For each killed mutant in the mutation results, give the selector a one-file diff on the mutated module; it must select the killer's file. Recall = selected killers ÷ killed mutants. Target 100% at file level. Each miss adds an edge or trigger plus a regression case in `tests/test_test_affected.py` (new).
2. *Shadow mode.* Each gate run records `test-affected --list --base <target>`. On a failing gate: recall = failing files selected ÷ failing files (quarantined failures counted apart).
3. *History.* Replay the last 30 merge candidates that have a failing receipt. Missing evidence is UNKNOWN, not a hit.

Because the gate stays full, a miss slows feedback but cannot merge a defect.

## 3. Flaky policy

### 3.1 Rules

- Zero retries: `retries: 0` written in every Playwright config, `retry: 0` in vitest, no rerun plugin in the lock, no shell rerun loop. A first failure stays a failure for that gate.
- Allowed: locator auto-waiting and polling one condition within a bounded timeout. Not allowed: re-running a test.
- One failure is a suspect. A flake is a case that both passed and failed at one SHA in one environment.
- Quarantine is a reviewed commit that changes the manifest entry and the test marker together. Never automatic.
- Tests listed in `tests/critical.txt` (new: the 13 sensitive files plus auth, privacy, media-ownership and isolation tests) cannot be quarantined until a deterministic replacement for the same invariant lands.

### 3.2 Format

`tests/quarantine.json` is the only source; it starts as `{"schema_version":1,"entries":[]}`. An entry:

```json
{"id": "Q-0001", "framework": "pytest",
 "selector": {"file": "tests/test_x.py", "nodeid": "tests/test_x.py::test_y[a]", "config": null, "title_path": null},
 "owner": "tnunamak", "lane": "azizam-tso-s4",
 "issue": "https://github.com/tnunamak/azizam/issues/N",
 "reason": "0.3 s join races the worker", "failure_signature": "AssertionError: expected cancelled",
 "quarantined_on": "2026-10-12", "expires_on": "2026-10-26", "evidence": ["<receipt log path>"]}
```

Markers: pytest `@pytest.mark.quarantine("Q-0001")` with `--strict-markers`; vitest title token `@quarantine @Q-0002`; Playwright tags `['@quarantine', '@Q-0003']`. One entry selects one collected case; file, suite and wildcard quarantine are forbidden. The gate deselects quarantined cases; the nightly run executes them. `owner` is the accountable human (only `tnunamak` today); `lane` is the agent lane doing the repair.

### 3.3 Detection

The gate records first attempts. Nightly random-order and repeat runs (1.3) report any pass-and-fail at one SHA with seed, order digest and log. Weekly, tests with p95 > 5× p50 are listed as latency suspects, not quarantined.

### 3.4 Exit

The fix explains the mechanism (clock, barrier, shared state, port) and adds a regression check. Then 30 consecutive passes at the fixed SHA over ≥ 3 seeds, alone and inside its file, then 3 clean nights. Remove the entry and marker in one commit and close the issue. Never widen a timeout or add a sleep to pass. An obsolete test is deleted with a reason, not left in quarantine.

### 3.5 CI guards (`policy` step, built in T0)

1. Schema, unique IDs, issue URL present, `expires_on ≤ quarantined_on + 14 days`. An expired entry **fails the gate**; renewal is a reviewed commit with new evidence.
2. Manifest and collected markers match one to one (pytest `--collect-only`, `vitest list --json`, `playwright test --list` on all three configs).
3. Retry ban: parse configs for any non-literal-0 retry; fail on `pytest-rerunfailures`/`flaky` in the lock or a set `PYTEST_ADDOPTS`; reporters assert retry index 0.
4. Discovery: every `test_*.py`, `*.spec.{ts,tsx,mjs}`, `*.e2e.ts`, `*.playwright.*`, `*.household.ts`, `*.test.mjs` is collected by a gate or scheduled config, or listed with a reason in `tests/suites.toml`.
5. Skips: a gate skip must be on an allowlist with a reason (6 today). A new skip fails.
6. Self-tests: injecting a retry, an orphan, an expired entry or a stray marker each makes the step fail.

## 4. Oracles

Rule for all areas: mock the process boundary (HTTP transport, clock, subprocess, randomness), not our own functions; call production code; assert the complete value. The 725 `patch.object(proxy, …)` uses are a ratchet: the count may fall, never rise.

### 4.1 Golden files

One harness: `scripts/golden --check [surface]` in the gate; `scripts/golden --update <surface> [--case c]` by hand only, never in CI. Sorted-key indented JSON, fixed synthetic inputs (clock, IDs, origin `https://gateway.test`). Normalizers name each field path they change; no regex scrubbing (`mature-oracles-scrub-*`). Privacy canaries run on the raw response before normalization. Two generations must be byte-identical.

Order:
1. Routes and OpenAPI: extend `headless_routes.json`/`headless_openapi.json` (exist) to the full `app.openapi()` for a minimal and a full fixture.
2. Model catalogs: extend `models-list-snapshot-2026-10.json` (exists) to `/v1/models`, `/anthropic/v1/models`, `/me/api/models/catalog`, each allowed and denied.
3. `/.well-known/azizam` and `summarize_capabilities` per audience (client, admin, internal).
4. Error envelopes per surface (OpenAI, Anthropic, Ollama, native): status, headers, body.
5. Protocol transcripts: Anthropic request → canonical upstream request → response or ordered stream events; Ollama NDJSON frames.
6. Receipt projections for member A, member B, owner, missing.

Kept as they are: `test_transforms_golden`, `test_lifecycle_swap_golden`, `dispatch_admission_baseline.json`, the comfy object-info catalogs.

### 4.2 Borrowed conformance and differential checks

The repo already holds `openai-openapi-responses*.json`, `anthropic-sdk-schemas.json` and `ollama-api-schemas.json` (the dot did not see them).

1. **Gate, first:** record each fixture's upstream URL, SHA, license and date, reproducible by `tests/fixtures/generate_*`. Fix M3: `test_openai_schema_conformance` must validate the gateway's own serializer, not a payload the test returned to itself. Validate every route `docs/SUPPORT_MATRIX.md` marks supported; rejection surfaces get rejection tests.
2. **Scheduled, then gate:** MCP conformance (`modelcontextprotocol/conformance`, pinned, `--requirements 2026-07-28` and `2025-11-25`) against a disposable instance. Promote to the gate after 14 clean nights.
3. **Gate, third:** official clients as differential oracles over `httpx.ASGITransport` (no network): OpenAI, Anthropic and `ollama` Python clients, pinned in the hermetic lock. One request corpus goes as raw HTTP and through each SDK; both must parse, with stream events in the same order.
4. **Gate, differential pairs:** Anthropic route vs OpenAI route on one fake engine (content, tool calls, usage); category-policy preview vs direct `decision_for`; `runtime_auth` hash vs `proxy.hash_api_key` (equality); the LiteLLM fake vs the real `LiteLLMAdminClient` contract.
5. Not adopted: A1111 upstream tests (AGPL, not a remote kit). vLLM, llama.cpp and LiteLLM suites are mined for single wire assertions only.

### 4.3 Property-based (Hypothesis, added to the hermetic lock)

Each property has an independent reference model in the test. The gate runs 100 examples; nightly runs 2,000 with a fixed seed. Shrunk counterexamples are kept as explicit examples.

| Order | Module | Property (dot ID) |
|---|---|---|
| 1 | `public_failure` (44%) | Every copy key yields a layer and message; unused details never change the message (P02). |
| 2 | `category_policy`, `category_enforcement` | Relational model of grant/deny/unknown; the overlay never widens a decision (P01, P06). |
| 3 | `transforms` | Pipeline = ordered composition; defaults fill only missing leaves; clamp, drop, value-map (P03, P07-P11). |
| 4 | `speech_voices` | Catalog agrees with value-map resolution (P04). |
| 5 | `policy` | Tightening an allow-list never grants; the first failed guard decides (P05). |
| 6 | `media_jobs`, `receipt_store` | State machines on a real temporary SQLite store: legal transitions, replay window, one terminal finalize (P13-P15). |

The dot found four gaps between code and the expected property: `PublicFailure.body()` passes `details` through; `default-params`/`value-map` share mutable config across requests; `voxtral-voices` maps unknown voices to `alloy` while it reports `allow_other=False`; legacy `record` can overwrite a terminal receipt. The slice writes a characterization test, then checks reachability from public routes. A real defect gets its own bug-fix PR, never a quiet change inside a test rewrite.

### 4.4 Real dependencies in place of mocks

- **State root:** pytest `--basetemp` and per-worker state on tmpfs (`$XDG_RUNTIME_DIR` or `/dev/shm`) with a size check and cleanup. No production pragma changes. Process-kill durability tests (`test_provider_crash_recovery`, `test_sqlite_startup`, one per store) keep a disk directory via a `real_fsync` marker.
- **Template databases:** migrate each store once per session, copy the file per test. Today function-scoped `gateway` fixtures migrate for every test (`test_catchall_never_dispatches`: 222 of 238 s is setup).
- **Module-scoped apps** for read-only cases, with table truncation; mutating cases keep a fresh copy.
- **Real stores:** admin audit paging (M1) on the real keyset SQL; receipt persistence (M4) on the real store. `FakeAdminStore` remains only for router wiring.
- **Loopback ASGI fakes** at the HTTP edge (LiteLLM, IdP `/token` + `/jwks`, engine) replace patches of `proxy` internals in the sensitive and route-policy files.
- **Clocks and barriers** replace outcome-deciding `sleep(` calls; `page.clock` replaces the 75 s and 30 s browser waits.
- **`tests/support/`** (new) replaces the cross-test imports of `test_admin_oidc`, `test_exposure_oidc_security`, `tests.test_admin_api`.

### 4.5 Mutation testing

- The harness keeps the `audit/mutation-raw` result format and refuses to score unless its baseline run is green.
- Core modules: `category_policy`, `category_enforcement`, `policy`, `public_failure`, `speech_voices`, `transforms`, `media_jobs`, `receipt_store`, `admin_store` (auth paths), `runtime_auth`.
- Survivors are classified (killable, equivalent, unproductive) in a file in the repo. Score = killed ÷ (all − equivalent).
- Uses: the proof for each rewritten or deleted test; a no-regression rule for core modules within overhaul slices; the scheduled report. Not a permanent gate (`mutation-testing-should-be-sampled-diff-aware-*`).

## 5. Deletions, merges, moves

Each needs its slice proof before merge.

| Item | Action | Reason |
|---|---|---|
| DD1-DD21 (`consolidated.md` 2.1) | Delete | Duplicate; the named survivor fails on the named mutant. |
| DA1-DA9 | Replace | Asserts nothing (tests a copy of production logic or its own socket). |
| `test_auth_inject` first-match test; `test_lan_sk_local…`/`test_unknown_virtual_key…`; two pairs in `test_litellm_key_endpoints`; sign-in provider-label rows | Delete or parametrize | Duplicate (`sensitive-audit.md`). |
| Cookie-forging half of `test_sec08_existing_owner…` | Delete | Duplicate of the real-callback test in `test_sec_auth_login.py`. |
| 17 `*.e2e.ts`, `app-shell.playwright.spec.ts` | Move unique assertions into gated `web/e2e/*.spec.ts`, then delete the copy and its area config | Run nowhere; the copies diverged, so a blind delete loses assertions. |
| `web/tests/ux/services-route.spec.mjs` | Delete | Replaced by `services-dialog.spec.tsx` :84/:121; source-regex test that no script runs. |
| `foundation`, `phone-overflow`, `public-surfaces` matrices (170 expanded cases) | One route × theme × width matrix; gate runs desktop-light + phone-dark, nightly runs all | Duplicate overflow checks. |
| Home, health-pill, status, provider-unknown, owner-alert, settings clusters (`consolidated.md` 2.2) | One vitest truth table per concept + one browser seam | Display states repeated in the browser. |
| Browser HTTP-only assertions (headers, redirects, range, tamper) | Move to API tests | The browser adds nothing. |
| `test_admin_render` JS/CSS strings; `models-client.spec.tsx` :1704 | Move to vitest DOM or browser | Pass when the rendering is dead code. |
| `test_user_render` :726 skip without Node | Fail instead | Silent skip in the gate. |
| `probe_openai.py` remote probes, `tests/compat/node/*` | Move to scheduled | Need live services; manual today. |
| 10 area Playwright configs in `web/src/**` | Delete after S13 | Wired to nothing. |

Not merged (different seams per the dot): the redaction layers, swap drain unit vs lifecycle, the prompt-memory family, origin-bound secret layers.

## 6. Budgets

`tests/budgets.toml` (new) holds ceilings. The `budget` step **fails** the gate when a collected count exceeds its ceiling. Time is noisy (load average 10-30 from other agents), so time ceilings are checked on the median of the last 5 gate runs in the nightly report. Raising a ceiling is a line in `budgets.toml` with a reason, in the same PR. The lead reviews budgets weekly from the scheduled report.

| Tier | Today | Ceiling after the overhaul |
|---|---|---|
| Python gate | 7,884 tests + 2,803 subtests; 2,430 test-s; 216 s alone at 12 workers | ≤ 8,000 tests; ≤ 1,400 test-s; ≤ 140 s |
| Vitest | 1,467 tests; 16 s | ≤ 1,800 (browser states move here); ≤ 30 s |
| Browser gate (main + live-data + household) | 737 + 4 + 15 = 756 cases; slowest lane 279-307 s | ≤ 560 cases; slowest lane ≤ 240 s |
| Gate wall | 421-488 s | p50 ≤ 360 s; ceiling 480 s |
| Local loop | UNVERIFIED | p50 ≤ 90 s gateway, ≤ 30 s web |
| Scheduled | none | nightly ≤ 60 min; weekly ≤ 3 h |
| Ratchets | 725 `patch.object(proxy…)`; 114 `sleep(` (upper bound); 6 gate skips; 19 files run by no gate | never rise; no-gate files 0 after S13 |

## 7. Slice order

One PR per slice, rebased on the current release, with files re-read there (r20b changed 206 files). **Proof for every slice:** (1) same pass set by test ID, except listed deletions; (2) each rewritten test fails on its named mutant or a revert of the fix it guards, and the old test passes on it; (3) each deleted test's survivor fails on its named mutant; (4) three green gates in random order; (5) core-module mutation scores before/after, none lower; (6) counts and test-seconds before/after.

| # | Slice | Extra proof | Expected before → after |
|---|---|---|---|
| T0 | Guards: empty quarantine, retry ban, discovery guard (orphans listed in `suites.toml` with S13 as exit), skip allowlist, `budgets.toml` at today's values, host lock, green-baseline mutation harness, `job-notices` fix | Each 3.5 self-test fails on injection; `job-notices` 0 failures in 20 shuffled runs | Blind spots visible |
| S0 | tmpfs state, template DBs | 5-run A/B; durability tests pass under `real_fsync`; two random-order full runs show no leak | 2,430 → ≤ 1,800 test-s (UNVERIFIED) |
| S1 | DD1-DD21, DA1-DA9 | Per-row mutants (`consolidated.md` 2.1) | −30 tests that cannot fail |
| S13 | Orphaned browser files | Per moved assertion, a UI mutation fails the gated spec | 19 → 0 no-gate files; ≤ +40 cases |
| S3+P1 | Enforcement, route policy; properties for `category_policy`, `category_enforcement`, `policy` | S3 mutants; `_report` fails on 400/422/500 | 472 → ≤ 200 test-s; 104 → ≤ 40 survivors |
| S2 | Admin plane on the real store; sensitive rewrites over ASGI fakes | Audit-paging and credential mutants; revoked key gives 401 through the loopback | 462 → ≤ 260 test-s |
| P2 | `public_failure`, `transforms`, `speech_voices` properties | Characterization tests for the 4.3 findings | `public_failure` 44% → ≥ 85% |
| S4+P3 | Prompt memory, job recovery; state machines | S4 mutants; barriers replace sleeps | 489 → ≤ 250 test-s; 61 s long pole gone |
| G1 | Golden harness: routes/OpenAPI, catalogs, discovery, errors | An added field, reordered list and leaked canary each fail | Repeated shape asserts merged |
| B1 | Schema provenance, M3 fix, SDK differential clients | A renamed serializer field fails the schema check and both SDKs | M3 closed |
| S10, S11 | Receipts and spend; Python renderers | S10/S11 mutants | 278 → ≤ 150 test-s |
| S6, S7 | Execution, providers; conformance, compat | Forwarder-removed and DONE-ignored mutants fail, not skip | 6 → ≤ 2 skips |
| S8, S9, S5a, S5b, S15 | Lifecycle, manifests, media (decoded pixels/PCM), platform | Per-slice mutants in `consolidated.md` | ~150 test-s (UNVERIFIED) |
| S12a-c | Vitest oracle rewrites | Classifier and seek mutants | 0 vitest flakes in 5 runs |
| S14+G2 | Browser matrix collapse, PR smoke set, `page.clock`, valid media bytes; MCP conformance into the gate after 14 clean nights | Reduced matrix still fails on 390 px overflow, a wrong theme token, a revoked key | 756 → ≤ 560 cases; lane ≤ 240 s |

Changes to the audit order: T0 precedes S0 so later slices are measured honestly; S13 moves to third because assertions that run nowhere are the biggest blind spot; the P, G and B slices are new and travel with the slice that owns the same files.

## 8. Success metrics for the finish report

Same harness before and after (5 runs, random order, load average recorded):

1. Wall time: local loop p50 (gateway, web), gate p50/p95, nightly, weekly.
2. Test count per tier: collected, deleted (with reason), added, moved between tiers.
3. Test-seconds and setup share (2,430 s, 35% today).
4. Mutation score on the ten core modules, green baseline, equivalents classified. Today: `category_policy` 75.0-81.9% (540/659, 46 kills from baseline-failing tests), `category_enforcement` 82.9%, `public_failure` 44.3%, `speech_voices` 85.3%; six UNVERIFIED.
5. Flakes: quarantine entries (0 today), first-attempt gate failures per 100 runs, nightly suspects.
6. Blind spots: no-gate files (19 → 0), gate skips (6), `patch.object(proxy…)` (725), `sleep(` (114).
7. Selector recall by mutant replay (target 100% file level).

## 9. UNVERIFIED

- Local-loop timings and every "saved" figure before the S0 A/B.
- `audit/measurements.md`, `audit/mutation.md`; mutation scores for six core modules.
- Why `test_route_policy_inventory` failed in the mutation baseline (environment or real).
- Location of `route_method_baseline.json` (the dot saw it at `6dc2598`; absent on this branch).
- Whether MCP conformance and the SDK corpora pass today; no upstream suite was run.
- Whether the four 4.3 findings are reachable from a public route.

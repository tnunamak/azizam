# Test strategy

Status: design v2, 2026-10-12. Nothing here is built unless a line says "exists".
Base: `chore/fast-gate` (release 18 + pytest-xdist, 10 browser lanes, `scripts/test-affected`).
Inputs: research in `~/code/divan/test-suite-excellence/` and `~/code/divan/testing/`; the audit in `~/Documents/azizam/test-overhaul/audit/` (`consolidated.md`, `sensitive-audit.md`, `measurements.md`, `mutation-raw/`, the `dot/azizam-tso-*` proposals); the v1 review `audit/dot/judge-design.md` (APPROVE WITH CHANGES, resolved in section 9). `audit/mutation.md` does not exist; scores below come from `mutation-raw/`.

Owner rules, applied everywhere:
- No automatic retries. A first failure stays a failure.
- No coverage gate. Count and time are budgets.
- Every behavior change carries a test that fails before the change and passes after it. A change with no behavior change says why no test applies.
- Every deleted test states its reason and names the test that still catches its defect.
- Browser lanes never install `node_modules`. `ci-local` installs once (`npm ci`) and hard-links it into each lane (exists, `ci-local` 202-214). Keep that.

## 0. Findings and decisions

| Finding | Decision |
|---|---|
| Strong suites gate the integrated candidate and run slow tiers on a schedule; affected-only selection is safe only with a full backstop (Shopify). | Three tiers. The PR gate is the **full** gate on the exact merge candidate. Selection serves the local loop only. |
| Kubernetes banned retries and moved flakes to owners. | Zero retries. Quarantine only through the contract in 3.3, built when first needed. |
| Python tier: 287 s wall (229-325) on 12 workers, CPU/wall 1.3. With the whole state root on tmpfs: 40 s, same 7,884 passes, test-seconds 2,430 → 244 (one run). | The tier is fsync-bound. S0 moves pytest's temp directory to tmpfs, measured by its own A/B (4.4). |
| Browser main config: 363 s, ~2,700 CPU-s per pass, 7.5× the Python CPU. Slowest lane 279-352 s. | After S0 the browser lanes set the gate wall. S14 is the time slice. |
| Flakes: Python 0 in 6 runs (order never shuffled); Playwright 0 in 3 (one fixed plan); vitest `job-notices.spec.tsx` 1 in 5 at load 35-40. | Fix `job-notices` (F0). Shuffled order arrives in T0, so the zero is not yet evidence. |
| 16 files run in no gate on this branch: 14 `*.e2e.ts` (≈57 tests), `app-shell.playwright.spec.ts`, `services-route.spec.mjs`. 10 area Playwright configs are wired to nothing. | A discovery guard fails on any test file no tier runs. Known orphans carry an expiry. |
| Mutation baselines for `category_policy`, `category_enforcement` and `policy` had 2 failing tests (`test_route_policy_inventory`). | Those scores are not "before" evidence. T0 re-establishes green baselines. |
| `sensitive-audit` (measurements 5): 3 auth files hide behavior behind mocks (`test_litellm_delegated_auth`, `test_litellm_key_endpoints`, `test_protocol_forward_auth`). | Rewritten over loopback ASGI fakes in S2. |

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
- Exclusive host lock (`flock ~/.tmp/azizam-ci.lock`): two gates share 24 cores and ports 18000-18998. Compose still finishes before browser lanes start (`ci-local` 147-152).
- **Signoff receipt (T0).** `--signoff` is accepted only when all of these hold, else it exits nonzero:
  1. This invocation ran every required step fresh. The tree cache (`ci-local` 50-54) stays for development runs and never yields a signoff.
  2. The receipt records SHA, target branch, target tip, toolchain fingerprint (Python, Node, Chromium, ffmpeg), seed, the required step set and the `policy` result. A partial or interrupted run writes no receipt.
  3. `HEAD^1` equals the recorded target tip, and `git ls-remote` still returns that tip just before `gh signoff create`.
  4. No open `nightly-red` issue exists, unless the PR closes it.
- Limit: GitHub does not verify a signoff and the repository has no branch protection (`docs/ops/CI.md`), so "merge only a SHA with a `signoff` status" is a rule, not a lock. Branch protection requiring `signoff` would make it a lock (owner decision; availability UNVERIFIED).

### 1.3 Scheduled tier on the release tip

`scripts/test-scheduled nightly|weekly` (T0). A scheduled pass is never gate evidence.

**Owner decision (reversible, default recorded 2026-10-12):** a systemd user timer runs it on the live machine at 04:00 local, `nice 19` and idle I/O class, at most 8 Python workers and 4 browser lanes, ports 18xxx only. The night is skipped if load average > 24 or a media job is in flight (read-only status query; endpoint UNVERIFIED). A skip is recorded as `SKIPPED`, never as a pass.

Nightly, 60 min, with provisional per-task allocations: web build 6; full Python, 3 seeds, 9; vitest, 5 seeds, 4; browser main config, one seed, 15; full browser matrix + sweep config + Firefox media project (missing Firefox fails) 20; live probes on a disposable 18xxx instance (`tests/compat/node`, `probe_openai.py` remote probes) 6. A task that runs out of time writes `INCOMPLETE`, never green.

Weekly, 3 h: mutation of the core modules (4.5), selector recall replay (2), borrowed conformance against latest upstream releases (4.2).

**Red night.** Any failure opens (or updates) a GitHub issue labelled `nightly-red` with seed, case IDs and receipt path. While it is open, signoff refuses (1.2). Diagnose the same day. If no bounded fix exists, revert the change that introduced it. Rerunning until green is not recovery.

Receipts: `~/.tmp/azizam-ci-receipts/` keeps gate and scheduled receipts for 90 days. A PR that cites evidence copies the needed lines into its description, so the evidence outlives the file.

## 2. Affected-only selection

`scripts/test-affected` (exists) stays the local selector and never feeds the gate. Slice L1 completes its contract:

- **Input.** Committed changes only (`merge-base..HEAD`), as today. This is the documented contract. `--changed-path P` (repeatable) replaces the git diff for mutant replay.
- **Output.** `--json` prints, per framework, `selected` (case or file IDs), `reason` per changed file, or `UNKNOWN` where the framework has no mapping.
- **Exit 2** when any changed production file selects no test ("no test reaches X"). Checked per file, so one selected file cannot hide another orphan.
- **Cross-stack.** A gateway change with no mapped web dependency selects the browser smoke set, not nothing.
- Whole-suite triggers (exist) gain: `tests/support/**`, fixtures no test names by path, `tests/budgets.toml`, `tests/suites.toml`, `scripts/test-affected`. Web: `web/e2e/fixtures/**`, `global-setup.ts`, any Playwright config.

**Recall** (weekly): for each killed mutant, replay the selector with `--changed-path <module>`. A mutant counts as recalled when the selection includes at least one of its killers. Recall = recalled ÷ killed. Each miss adds an edge or trigger and a case in `tests/test_test_affected.py`. Because the gate stays full, a miss slows feedback but cannot merge a defect.

## 3. Flaky policy

### 3.1 Rules

- Zero retries: `retries: 0` in every Playwright config, `retry: 0` in vitest, no rerun plugin in the lock, no shell rerun loop. Receipts record the attempt index; any index > 0 fails `policy`.
- Allowed: locator auto-waiting and polling one condition inside a bounded timeout.
- A flake is a case that passed and failed at one SHA in one environment. One failure is a suspect.
- **Diagnostic repetition is not a retry.** `scripts/test-repeat <case-id> N` (built with Q) writes its own receipt. It never changes a gate or nightly result, and a failed first attempt stays recorded.

### 3.2 Until quarantine exists

No quarantine tooling is built in advance. `policy` fails on any quarantine manifest or marker. A flake is fixed, or the change that introduced it is reverted (1.3). Build slice Q (3.3) when a flake has no fix within 2 days.

### 3.3 Quarantine contract (slice Q, on demand)

- **Source:** `tests/quarantine.json` only; no markers in test code. Fields: owner, issue, reason, failure signature, first-failure receipt, `expires_on` ≤ 14 days (expired fails the gate).
- **Identity:** framework + config/project + full collected case ID (pytest node ID with parameters; vitest file + title path; Playwright config + project + file + title path). `policy` resolves each entry against the collected lists; zero or several matches fail, so file and suite entries fail. The runner removes exactly that case (pytest `--deselect`, Playwright `--test-list`, vitest a setup hook; the vitest mechanism is UNVERIFIED). Nightly still runs it.
- **Protected invariants:** for a case in `tests/critical.txt` (the 13 sensitive files, auth, privacy, media ownership, isolation), the entry also names the `invariant` and a gate `replacement` case, with a receipt where the replacement fails on a named mutant or revert and passes clean.
- **Exit:** the fix names the mechanism (clock, barrier, shared state, port) and adds a regression check. Then 30 first-attempt passes at the fix SHA (10 alone, 10 in its file, 10 in full random-order runs, ≥ 3 seeds), each receipt with seed, order digest, environment and SHA. Never widen a timeout or add a sleep to pass.
- **Negative tests:** ambiguous or zero match, file-scope entry, critical case without replacement, missing or wrong-SHA exit evidence, expired entry, runtime retry. Each must fail `policy`.

## 4. Oracles

Rule for all areas: mock the process boundary (HTTP transport, clock, subprocess, randomness), not our own functions; call production code; assert the complete value. The 725 `patch.object(proxy, …)` uses are a ratchet: the count may fall, never rise.

Priority: first repair self-fulfilling oracles inside the slice that owns them (M3 real-serializer check, M1 audit paging and M4 receipts on real stores, privacy regressions). Then small stable goldens (catalogs, error envelopes). Broad OpenAPI and protocol transcripts come last.

### 4.1 Golden files

One harness: `scripts/golden --check [surface]` in the gate; `--update` by hand only. Sorted-key JSON, fixed synthetic inputs (clock, IDs, origin `https://gateway.test`). Normalizers name each field path they change; no regex scrubbing. Privacy canaries run on the raw response before normalization. Two generations must be byte-identical.

G1: model catalogs (`/v1/models`, `/anthropic/v1/models`, `/me/api/models/catalog`, each allowed and denied), `/.well-known/azizam` per audience, error envelopes per surface. G2: full `app.openapi()` (minimal and full fixture), Anthropic and Ollama transcripts, receipt projections per member. Kept: `test_transforms_golden`, `test_lifecycle_swap_golden`, `dispatch_admission_baseline.json`, the comfy object-info catalogs.

### 4.2 Borrowed conformance and differential checks

1. **Gate (B1):** record each schema fixture's upstream URL, SHA, license and date (`openai-openapi-responses*.json`, `anthropic-sdk-schemas.json`, `ollama-api-schemas.json`, reproducible by `tests/fixtures/generate_*`). Fix M3: `test_openai_schema_conformance` validates the gateway's own serializer output.
2. **Gate (S7):** official OpenAI, Anthropic and `ollama` Python clients over `httpx.ASGITransport`. SDK parsing proves interoperability only, so each case also asserts normalized content, tool calls, usage and the ordered event list.
3. **Gate, differential pairs:** Anthropic vs OpenAI route on one fake engine; category-policy preview vs `decision_for`; `runtime_auth` hash equals `proxy.hash_api_key`; the LiteLLM fake vs the real `LiteLLMAdminClient` contract.
4. **Scheduled (weekly), then gate:** MCP conformance (`modelcontextprotocol/conformance`, pinned). Promote only when its measured runtime fits the gate budget and it catches at least one seeded defect. Upstream compatibility today: UNVERIFIED.
5. Not adopted: A1111 upstream tests (AGPL). vLLM, llama.cpp and LiteLLM suites are mined for single wire assertions.

### 4.3 Property-based (Hypothesis, in the hermetic lock)

The reference is an independent model where one exists, otherwise a metamorphic relation. Copying production logic into the test is not an oracle. Gate: 100 examples; nightly: 2,000 with a fixed seed; shrunk counterexamples stay as explicit examples.

| Slice | Module | Property |
|---|---|---|
| P1 | `category_policy`, `category_enforcement`, `policy` | Relational grant/deny/unknown model; the overlay never widens; tightening an allow-list never grants; the first failed guard decides. |
| P2 | `public_failure` | See below. |
| P2 | `transforms`, `speech_voices` | Pipeline = ordered composition; defaults fill only missing leaves; catalog agrees with value-map resolution. |
| P3 | `media_jobs`, `receipt_store` | State machines on a real temporary SQLite store: legal transitions, replay window, one terminal finalize. |

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
- Loopback ASGI fakes at the HTTP edge (LiteLLM, IdP `/token` + `/jwks`, engine) replace patches of `proxy` internals in the sensitive and route-policy files.
- Clocks and barriers replace outcome-deciding `sleep(` calls; `page.clock` replaces the 75 s and 30 s browser waits.
- `tests/support/` replaces the cross-test imports of `test_admin_oidc`, `test_exposure_oidc_security`, `tests.test_admin_api`.

### 4.5 Mutation testing

- `scripts/mutate <module>` (T0, from the audit harness) refuses to score unless its baseline run is green. It writes each mutant's ID, status and killer case IDs.
- Core modules: `category_policy`, `category_enforcement`, `policy`, `public_failure`, `speech_voices`, `transforms`, `media_jobs`, `receipt_store`, `admin_store` (auth paths), `runtime_auth`.
- Survivors are classified (killable, equivalent, unproductive) in a repo file. Score = killed ÷ (all − equivalent).
- Uses: named mutants for the slice ledger (section 7); the weekly report; final milestones. Never a gate.

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
| `probe_openai.py` remote probes, `tests/compat/node/*` | Move to scheduled | Need live services. |

Not merged (different seams): the redaction layers, swap drain unit vs lifecycle, the prompt-memory family, origin-bound secret layers.

## 6. Budgets

Every target is provisional until measured on this host at the recorded load. Time is observational: the weekly report shows the median of the last 5 gate runs; no gate fails on time.

**Count units:** pytest collected items (each parameter set counts; subtests reported, not counted); vitest test cases; Playwright test × project. Quarantined cases count before deselection, so quarantine cannot buy budget.

**Count ceilings** in `tests/budgets.toml`, enforced by `policy` from T0. T0 sets them at today's counts. A slice that must add cases (S13 adds ≤ 40 browser cases) raises the ceiling in the same PR with a reason; S14 lowers it. No other raises.

| Tier | Today (measured) | Provisional target |
|---|---|---|
| Python gate | 7,891 collected + 2,803 subtests; 287 s wall (229-325); 2,430 test-s | ≤ 8,000 collected; wall and test-s set from the S0 A/B |
| Vitest | 1,467 tests; 19 s (16-20) | ≤ 1,800 (browser states move here); ≤ 30 s |
| Browser gate (main + live-data + household) | 737 + 4 + 15 = 756 cases; main 363 s on 6 measurement lanes, slowest lane 279-352 s | ≤ 560 cases after S14; slowest lane ≤ 240 s |
| Gate wall | 421-488 s (before S0) | p50 ≤ 360 s, reviewed after S0 and S14 |
| Local loop | UNVERIFIED | p50 ≤ 90 s gateway, ≤ 30 s web |
| Scheduled | none | nightly 60 min, weekly 3 h, per-task allocations (1.3) |
| Ratchets | 725 `patch.object(proxy…)`; 114 `sleep(` (upper bound); 6 gate skips; 16 no-gate files | never rise; no-gate files 0 after S13 |

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

**Extra for slices that change shared fixtures, isolation or runner state (S0, `tests/support/`, Q) and for milestones:** three full random-order runs with recorded seeds, and the mutation diff over all core modules.

| # | Slice | Extra proof |
|---|---|---|
| F0 | `job-notices.spec.tsx` flake fix (bounded explicit wait or fake clock), own PR | 0 failures in 20 shuffled runs under load |
| T0 | Prerequisites (below) | Each `policy` self-test fails on injection; signoff refuses in each case of 1.2; two seeds give identical item order on every xdist worker |
| S0 | `TMPDIR` on tmpfs, `host_disk_dir` fixture | The A/B in 4.4 |
| L1 | Selector contract (section 2) | Exit 2 on a per-file orphan; `--changed-path` replay matches git-diff selection |
| S1 | DD1-DD21, DA1-DA9 | Ledger rows from `consolidated.md` 2.1 |
| S13 | Orphaned browser files | Per moved assertion, a UI mutation fails the gated spec; ceiling raise ≤ 40 |
| S3+P1 | Enforcement, route policy; P1 properties | S3 mutants; `_report` fails on 400/422/500 |
| S2 | Admin plane on the real store (M1, M4); sensitive rewrites over ASGI fakes | Audit-paging and credential mutants; revoked key gives 401 through the loopback |
| B1 | Schema provenance, M3 fix | A renamed serializer field fails the schema check |
| P2 | `public_failure`, `transforms`, `speech_voices` | The four characterizations in 4.3; owner policy recorded |
| G1 | Golden harness: catalogs, discovery, error envelopes | An added field, a reordered list and a leaked canary each fail |
| S4+P3 | Prompt memory, job recovery; state machines | S4 mutants; barriers replace sleeps |
| S10, S11 | Receipts and spend; Python renderers | S10/S11 mutants |
| S6, S7 | Execution, providers; conformance, compat; SDK differential clients | Forwarder-removed and DONE-ignored mutants fail, not skip |
| S8, S9, S5a, S5b, S15 | Lifecycle, manifests, media (decoded pixels/PCM), platform | Per-slice mutants in `consolidated.md` |
| S12a-c | Vitest oracle rewrites | Classifier and seek mutants |
| G2 | Full OpenAPI, protocol transcripts, receipt projections | As G1 |
| S14 | Browser matrix collapse, PR smoke set, `page.clock`, valid media bytes; MCP conformance if 4.2.4 holds | Reduced matrix still fails on 390 px overflow, a wrong theme token, a revoked key; ceiling lowered |
| Q | Quarantine tooling (3.3), only when 3.2 triggers it | Q's negative tests |

Per-slice test-second targets from v1 were disk-era figures; they are reset from the S0 A/B.

**T0 contains exactly what later slices depend on:**
1. **Order seed:** `ci-local --order-seed N` (rejected today). A conftest hook shuffles pytest items from the seed, identically on every xdist worker; vitest `--sequence.shuffle --sequence.seed`; the lane planner seeds file-to-lane assignment.
2. **Case receipts:** pytest `--junitxml`, vitest and Playwright JSON merged into `cases.jsonl` (framework, config/project, case ID, outcome, attempt index, duration, seed, SHA).
3. **Signoff receipt** with the refusals in 1.2, and the host lock.
4. **`policy`:** retry ban; discovery guard with orphans in `tests/suites.toml`, each with `expires_on` (S13 clears them); skip allowlist (6); no quarantine markers; count ceilings. Self-tests inject each violation.
5. **`scripts/mutate`** (4.5), and green baselines for `category_policy`, `category_enforcement`, `policy` once the `test_route_policy_inventory` baseline failure is explained.
6. **`scripts/test-scheduled`** with the 04:00 timer and owner limits, `SKIPPED`/`INCOMPLETE`, `nightly-red`, 90-day retention.

Order rationale: F0 removes the one known flake before T0 starts shuffling. T0 precedes S0 so S0 is measured with seeds and case receipts. S0 comes next because the tmpfs change touches every test's runtime and every later timing. S13 stays early because assertions that run nowhere are the largest blind spot. Oracle repairs travel with the slice that owns the files.

## 8. Success metrics for the finish report

Same harness before and after (5 runs, random order, load recorded):

1. Wall time: local loop p50 (gateway, web), gate p50/p95, nightly, weekly.
2. Counts per tier in the units of section 6: collected, deleted (with reason), added, moved.
3. Test-seconds and setup share, against the S0 A/B baseline.
4. Mutation score per core module from a green baseline, equivalents classified. Clean today: `public_failure` 44.3% (70/158), `speech_voices` 85.3%. Not comparable (2 failing baseline tests): `category_policy` 540/659, `category_enforcement` 82.9%, `policy` 238/290. Not measured: five modules.
5. Flakes: first-attempt gate failures per 100 runs, nightly suspects, quarantine entries.
6. Blind spots: no-gate files (16 → 0), gate skips (6), `patch.object(proxy…)` (725), `sleep(` (114).
7. Selector recall by mutant replay.

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

## 10. UNVERIFIED

- Every saving before the S0 A/B; the backing of each path inside the sandbox (static reading only).
- Branch protection availability for the `signoff` status.
- The read-only endpoint for "media job in flight".
- The vitest exact-case deselection mechanism (only needed by Q).
- Why `test_route_policy_inventory` failed in the mutation baselines (environment or real).
- Location of `route_method_baseline.json` (seen at `6dc2598`; absent on this branch).
- Whether MCP conformance and the SDK corpora pass today.
- Whether a public route passes secret-bearing values into `PublicFailure.details`.
- Scheduled allocations in 1.3; none has been run under the owner's limits.

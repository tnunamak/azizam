# CI

The routine gate runs on the owner's machine, not on GitHub-hosted runners.

## Why

Until 2026-10-10 every push to every branch, and every pull request, ran all
four hosted jobs: about 40 minutes of runner time per push (run 37696958583:
30 minutes of it the browser journey), plus about 46 MB of browser diagnostics uploaded on every run
and kept for 7 days. Review branches pushed only so another agent could read a
commit triggered the same full run. From 2026-10-07 GitHub stopped starting
hosted jobs ("recent account payments have failed or your spending limit needs
to be increased"). 88 diagnostic artifacts (8.0 GB)
were still stored.

## How it works now

- `scripts/ci-local` runs the same four jobs as `.github/workflows/ci.yml`
  (web check, Python tests, fresh instance, production UI journey) on HEAD,
  in a clean detached worktree with `npm ci` and fresh Python environments
  from `tests/requirements-hermetic.lock`.
- It writes receipts to `~/.tmp/azizam-ci-receipts/<tree>/`: `run.tsv`
  (SHA, seed, layout), `steps.tsv` (step, exit code, seconds), `logs/`,
  `raw/` (each framework's report), `cases.jsonl`, and browser evidence only
  when a run fails. A tree that already passed is not run again (`--force`
  reruns it).
- Its worktrees and dependency copies live in `~/.tmp/azizam-ci-local.*`
  (about 7 GB). Exit removes that directory, except after a failed run:
  ci-local then keeps it, prints its path and records it in
  `~/.tmp/azizam-ci-local.kept`; the next failed run deletes it. A run
  directory left by a killed `ci-local` is deleted by the next run.
- Only one `ci-local` runs at a time on the host: it holds
  `flock ~/.tmp/azizam-ci.lock` for the whole run, and a second run waits
  (the lock file names the last `ci-local` that held it). Every job inherits
  the lock, so if `ci-local` itself is killed, the host stays locked until
  its surviving jobs end; `fuser -v ~/.tmp/azizam-ci.lock` lists them. Do
  not wrap `ci-local` in `flock` on the same file: the inner lock would wait
  for the outer one forever.
- The hosted workflow runs only when started by hand
  (`gh workflow run CI --ref <branch>`). It uploads diagnostics only when a
  job fails, keeps them 1 day, and cancels an older run on the same ref.

## Order seed

Every run shuffles test order from a seed: `--order-seed N`, or a random
seed that the run prints and records in `run.tsv`. The same seed gives the
same order, so `scripts/ci-local --force --order-seed N` reproduces a failure.

- pytest: `--order-seed N` (a `tests/conftest.py` hook) shuffles modules,
  the classes in each module and the tests in each class. The order depends
  only on the seed and the node IDs, so every xdist worker collects the same
  list (xdist aborts the run when two workers' ordered lists differ).
- vitest: `--sequence.shuffle --sequence.seed N`.
- Playwright: the main run is one invocation. Parallel workers take tests in
  the order Playwright assigns; every test gets its own namespace (the
  `isolated` fixture, `web/e2e/fixtures/isolation.ts`), so a neighbor cannot
  change its data. Tests that write owner-wide settings carry `@global-state`
  and run serially on a second server. `--order-seed` does not reorder browser
  tests; it only goes into the namespace text (`UI_E2E_RUN_ID`). The browser
  tier's protection against bad neighbors is the data isolation above, which
  `scripts/e2e/prove-isolation` checks with four copies of each isolated case on
  four workers.

## Case receipts and policy

The `cases` step merges the reports into `cases.jsonl`, one line per
attempt: framework, config, project, case ID, file, outcome, attempt index,
duration, seed and SHA (`scripts/ci/merge_cases.py`). pytest writes JUnit
XML (`run-hermetic-pytest.sh --junit-xml-out`, which refuses an existing
file). vitest and Playwright use the repository's own reporters,
`web/tests/case-receipts.ts` and `web/e2e/case-receipts.ts`, which append a
line per attempt to `$CASE_RECEIPTS_FILE`: a framework retry, or a second
run into the same file, repeats a test ID, and every repeat is an attempt
above 0. Without `CASE_RECEIPTS_FILE` they write nothing.

The `policy` step (`scripts/ci/policy.py`) fails the run when:

- a retry exists: a rerun plugin in the hermetic lock, a flaky or rerun
  marker in Python test code (`conftest.py` and `tests/**` included), a
  `--retries`/`--retry`/`--reruns` flag in the gate scripts, any `retry`,
  `retries` or `repeats` value in web test code or a test config that is not
  the literal `0` (a computed value fails), a Playwright config the run used
  without `retries: 0`, the vitest config without `retry: 0`, or an attempt
  index above 0;
- a test file ran in no gate step and is not listed in `tests/suites.toml`
  as a `[[scheduled]]` file (run by `scripts/test-scheduled`, below), a
  `[[live]]` file (needs real upstreams) or an `[[orphan]]` with an
  unexpired `expires_on`. No `[[orphan]]` is left; a new unrun test file
  fails until it is wired into a gate step or listed;
- a case skipped (or xfailed) without a `[[skip]]` (`[[xfail]]`) entry for
  its framework, config, project and case ID in `tests/suites.toml`;
- a quarantine manifest or marker exists (TEST_STRATEGY.md 3.2);
- a framework's first-attempt case count exceeds `tests/budgets.toml`.

An entry that no longer matches the run (the orphan now runs, the case no
longer skips) also fails, so the lists only shrink. `tests/test_test_policy.py`
injects each violation into a copy of the tree and checks that the policy
fails, and that the clean copy passes. The policy reads source text, not the
resolved configuration: a retry set somewhere it does not scan is caught
only by the attempt index in the receipts.

## Scheduled tier

`scripts/test-scheduled nightly` runs what the gate cannot afford, on a
detached worktree of `--ref` (default `HEAD`): the Node SDK probes
(`tests/compat/node`) against a disposable gateway on 185xx whose upstream is
the scenario-driven fake upstream, and the quality sweep. The files it runs
are the `[[scheduled]]` entries of `tests/suites.toml`. The receipt is
`~/.tmp/azizam-ci-receipts/scheduled/nightly-<UTC time>/receipt.json`. The
outcome is PASS (exit 0), FAIL (1), SKIPPED (3: load over 24, or a job in the
media queue) or INCOMPLETE (4: a task timed out, could not start, or a listed
file ran no case or skipped one). Only PASS exits 0, and a scheduled pass is
never gate evidence. Each task takes the host lock alone and starts no task
while a gate waits (`~/.tmp/azizam-ci.gate-waiting`). The units
`docs/ops/azizam-test-nightly.{service,timer}` run it at 04:00 local; the
owner enables them.

## Signoff

`scripts/ci-local --force --signoff --target main [--closes-nightly-red N]`
runs on the merge candidate (the target tip with the change merged) and posts
the `signoff` commit status that `gh signoff create` posts, but to the tested
SHA by name (`gh api .../statuses/<sha>`), so GitHub must already have that
commit: push the candidate to a branch other than the target before the run
(the run refuses before any step if GitHub does not have the SHA). It refuses with exit 3, posts nothing and writes no signoff receipt,
unless all of these hold (TEST_STRATEGY.md 1.2):

1. `--force`: every required step runs fresh in this invocation, never from
   the tree cache; `--skip-compose` is refused.
2. `HEAD^1` is the tip of `origin/<target>` (`git ls-remote`), checked before
   the run and again, unchanged, before posting; the checkout's `HEAD` is
   still the tested SHA.
3. No open GitHub issue labelled `nightly-red`, except the one number given
   with `--closes-nightly-red`, and only when a commit in
   `<target tip>..<sha>` closes it in GitHub's words (`Fixes #N`,
   `Closes #N`, `Resolves #N`).
4. Every expected step, `policy` included, passed.

After GitHub accepts the status, ci-local writes `signoff-<sha>.tsv` in the
run's receipt directory: SHA, tree, target, target tip, seed, start and
finish, the step set, the policy summary, the closed `nightly-red` issue,
the toolchain (both Python environments, Node, Chromium, ffmpeg), and the
post time and status URL. A refused, interrupted or failed run writes none.

`tests/test_ci_local.py` runs the real `ci-local` in a throwaway repository
with stub runners and a local bare origin, and checks the receipt, each
refusal that can only happen after the tests pass (target or checkout moved),
the `nightly-red` closing commit, the kept failed-run directory, an
interrupted run, lock waiting, and a killed controller.

## Parallel local layout

- At start, three jobs run side by side, each in its own clean detached
  worktree: the Python hermetic suite; the fresh-instance venv and Compose
  journey; and web install, then unit tests beside check/build, then the
  browser venv, Chromium and the lane plan.
- The Python suite runs under pytest-xdist: `--py-workers N` (default `12`)
  workers with `--dist worksteal`. Each worker keeps its gateway stores in a
  private subdirectory of the run's state directory. The serial and parallel
  runs pass the same tests (7884 passed, 6 skipped, 1 xfailed, 2803 subtests
  on 2026-10-11).
- The Compose journey creates and removes a Docker network. Chromium aborts
  in-flight requests on any host network change (`net::ERR_NETWORK_CHANGED`),
  so the browser lanes start only after Compose finishes. The native
  fresh-instance journey runs beside the lanes; it needs no Docker network.
- `--browser-workers N` (default `9`) and `--browser-servers N` (default `3`)
  size the main run. It is one `playwright test` invocation with up to four
  servers: the parallel pool (project `chromium`, N workers spread over the
  pool's servers by worker index) on ports `BASE+10`, `BASE+50`, `BASE+60`,
  and the `@global-state` project (`chromium-global-state`, 1 worker, 33 cases
  that write owner-wide settings, services, profiles, categories or providers)
  on `BASE+20`. The live-data (`BASE+30`) and household (`BASE+40`) runs start at
  the same time. One gateway is one Python process: with 6 or 10 workers on a
  single gateway the run took 930-1000 s and failed on timeouts, so the pool
  has three. Each server is one gateway and one fake engine for its workers.
  Per-test data comes from the `isolated` fixture: a namespaced
  member, app, key and request log created through the admin API. Teardown
  fails a test that leaves queued or running jobs, open fake-engine requests,
  or a changed owner-wide setting it did not tag `@global-state`. The fixture
  snapshots seven owner-wide settings before each test and restores them after.
  `scripts/ci/policy.py` fails a gated spec that takes `test` from `@playwright/test`
  instead of `./fixtures/isolation`.
- `--port BASE` defaults to `18640`. Each server reserves gateway +0, fake
  engine +1 and strict-engine candidates +2 through +9. The entire allocation
  must fit within 18000–18998 or the command fails early. The fresh-instance
  journeys take the first free ports from 18000 upward.
- The browser steps share one completed web directory, read-only: no per-lane
  copies and no npm install or build while they run. The runner writes
  (`test-results`, auth files, screenshots, reports) go to the run directory;
  `web/test-results` is a symlink to it. The step `e2e-tree-unchanged` fails
  the run if any other file in `web/` changed.
- Every step writes a private receipt and log. The parent waits for every job,
  aggregates receipts, rejects missing/nonzero/skipped steps, and retains all
  failing-run browser evidence. Interrupts terminate job process groups
  before worktrees are removed. `setsid` is required.

## Duration

Browser tier on one host at the same priority (nice 19, idle I/O), measured on
2026-10-12 with a transient systemd scope: wall seconds, CPU seconds from the
scope's cgroup `cpu.stat`, 735 case rows, load average at start and end. The
host was shared with other agents (load 13-40).

| Layout | Wall | CPU | Cases / first-try failures | Gateway+engine pairs |
|---|---|---|---|---|
| before: 10 shard lanes + live-data + household, copies of `web/` | 342 s | 3,321 | 735 / 0 | 12 |
| after: 1 pool server, 6 workers | 999 s | 3,736 | 735 / 4 | 4 |
| after: 1 pool server, 10 workers | 931 s | 4,879 | 735 / 3 | 4 |
| after: 3 pool servers, 9 workers (default) | 480 s | 3,033 | 735 / 0 | 6 |

The slowest part of the "after" run is the serial `@global-state` project
(about 255 s of test time) plus the pool. The rest of the gate (Python tier)
is unchanged.

## Priority

`scripts/ci-local`, `scripts/run-hermetic-pytest.sh`, `scripts/test-affected` and the `web/` `test:components` and `test:e2e` scripts re-run themselves at `nice -n 19` and idle I/O class (`scripts/low-priority`), so test load does not slow the live gateway on the same machine. Set `AZIZAM_TEST_PRIORITY=normal` to skip this on a quiet machine.

## Affected tests for lanes

`scripts/test-affected [--base REF] [--list] [--e2e]` runs only the tests that
the commits since the merge base with REF (default `origin/main`) can reach:

- Python test modules whose static import closure contains a changed `.py`
  file, changed test modules, and test modules that name a changed file by
  path (or a changed non-Python file by name). A change to the shared harness
  (conftest, runner, lock files, `proxy_config.yml`) selects the whole suite.
  The selection runs through the hermetic runner with xdist, so commit first.
- `vitest run --changed` when `web/` changed, and with `--e2e` the Playwright
  specs that `--only-changed` selects (build `web/` first).

Most gateway modules reach `proxy.py`, so a gateway change still selects most
Python modules (145–231 of 324 on recent commits). It is a fast check
while working, not a gate: `scripts/ci-local` still runs everything.

## Limits

- A signoff is the owner's attestation that the local run passed. GitHub does
  not verify it, and it cannot be a required check while the repository has
  no branch protection.
- The policy finds test files by name (`test_*.py`, `*_test.py`,
  `*.spec|test|e2e.*`, `*.household.ts`). A test file with another name is
  invisible to the discovery check.
- The fresh-instance Compose journey needs Docker networks. If Docker cannot
  allocate one ("Pool overlaps with other one on this address space"), the
  run fails. `--skip-compose` records the step as skipped, and the run then
  cannot pass.

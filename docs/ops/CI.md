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
  in clean detached worktrees with private dependencies from the installed
  cache described below. Cache misses use `npm ci` and the hashed Python lock;
  the fresh-instance and hermetic Python tiers always install clean environments.
- It writes receipts to `~/.tmp/azizam-ci-receipts/<tree>/`: `run.tsv`
  (SHA, seed, layout), `steps.tsv` (step, exit code, whole seconds, wall
  seconds, CPU seconds (user plus system), peak RSS kB), `cost.json` (totals,
  the `journey` section, and the critical path: the longest chain of steps, each starting when the
  steps it waits for finish), `logs/`, `raw/` (each framework's report),
  `cases.jsonl`, and browser evidence only when a run fails. A tree that
  already passed is not run again (`--force` reruns it).
- Each step is measured by GNU time (`/usr/bin/time -q -o`), or by
  `scripts/ci/gate_cost.py run` (wait4) when GNU time is absent
  (`AZIZAM_CI_TIME` sets its path). Both report the step's own rusage, which
  includes the children it waited for. Peak RSS is the largest single
  process's peak, not the sum over processes alive at once. The run prints
  its cost table at the end. `scripts/gate-cost [-n N]` prints the median and
  worst cost of each step over the newest N finished runs. Pruning keeps
  `steps.tsv` and `cost.json` only for the newest 20 runs and recent signoffs,
  so older runs drop out of `gate-cost`.
- Its worktrees and dependency copies live in `~/.tmp/azizam-ci-local.*`
  (about 7 GB). Exit removes that directory on success and failure. Logs and
  case receipts survive under the receipts directory. A run directory left by
  a killed `ci-local` is deleted by the next run.
- At the start of each run, receipts are pruned (`scripts/ci/prune_receipts.py`):
  the newest 20 runs, and any run with a signoff receipt from the last 7 days,
  keep everything. Older runs keep only `run.tsv`, `PASS`, `FAIL` and
  `signoff-*.tsv`.
- Every test harness deletes its own scratch directory when it ends (trap on
  EXIT, HUP, INT and TERM). A SIGKILL runs no trap, so each directory holds a
  `.scratch-owner` marker (`<pid>-<start time>`) and the next start of that
  harness deletes the directories whose owner is dead
  (`scripts/lib/scratch.sh`, `scripts/scratch_owner.py`). A new harness that
  makes a scratch directory must use one of them. Fixture gateways and copies
  of owner data must never outlive the run.
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

The third task, `perf`, runs `scripts/perf/run.py` on a read-only copy of the live
databases (ports 18520-18521): every GET route and every page against
`scripts/perf/budgets.toml`. It lists no test files; over budget is FAIL and opens
or updates the open GitHub issue labelled `perf-red`. That label does not block
signoff. The gate holds the counts (SQLite statements and processes per request at
two data sizes) in `tests/test_route_work_budget.py` and `tests/test_work_budget.py`.
To investigate: `scripts/perf/README.md`.

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

- At start, four jobs run across three clean detached worktrees: hermetic
  Python; the fresh-instance venv and Compose journey; web dependency setup,
  followed by unit tests, build, browser venv and Chromium in parallel; and
  Node compatibility, Pi and MCP conformance checks in the shared web tree.
- The Python suite runs under pytest-xdist: `--py-workers N` (default `12`)
  workers with `--dist worksteal`. Each worker keeps its gateway stores in a
  private subdirectory of the run's state directory. The serial and parallel
  runs pass the same tests (7884 passed, 6 skipped, 1 xfailed, 2803 subtests
  on 2026-10-11).
- The Compose journey creates and removes a Docker network. Chromium aborts
  in-flight requests on any host network change (`net::ERR_NETWORK_CHANGED`),
  so the browser lanes start only after Compose finishes. The native
  fresh-instance journey runs beside the lanes; it needs no Docker network.
- `--browser-workers N` (default `10`) and `--browser-servers N` (default `2`)
  size the main run. It is one `playwright test` invocation with up to six
  servers: the parallel pool (project `chromium`, N workers spread over the
  pool's servers by worker index) on ports `BASE+10`, `BASE+50`, `BASE+60`,
  and two serial `@global-state` projects, each with one worker and a server of
  its own (`chromium-global-state` on `BASE+20`, `chromium-global-state-2` on
  `BASE+70`). These cases write owner-wide settings, services, profiles,
  categories or providers. `SECOND_GLOBAL_FILES` in `web/playwright.config.ts`
  names the files that run on the second server; any other file,
  including a new one, runs on the first. The two projects are listed before
  `chromium`, so they start with the pool and run beside it. `chromium-first-run`
  (`BASE+80`, one case) runs `first-run.spec.ts` on a gateway that global setup
  does not seed: no owner, no engine. Beside it run a LiteLLM stand-in (`+1`) and
  the engine the test adds (`+2`). The live-data
  (`BASE+30`) and household (`BASE+40`) runs start at the same time. One
  gateway is one Python process: with 6 or 10 workers on a single gateway the
  run took 930-1000 s and failed on timeouts. Each server is one gateway and one
  fake engine for its workers.
  The cases of one file run in parallel in `chromium` (`fullyParallel`). A file
  whose cases must stay in order or in one worker says so with
  `test.describe.configure({ mode: "default" })`. The Try files do: Try mints one
  key per sign-in and revokes the session's older Try keys, so two Try cases on
  one server at once fail with "Invalid API key".
  Per-test data comes from the `isolated` fixture: a namespaced
  member, app, key and request log created through the admin API. Teardown
  fails a test that leaves queued or running jobs, open fake-engine requests,
  or a changed owner-wide setting it did not tag `@global-state`. The fixture
  snapshots seven owner-wide settings before each test and restores them after.
  `scripts/ci/policy.py` fails a gated spec that takes `test` from `@playwright/test`
  instead of `./fixtures/isolation`.
- `--port BASE` defaults to `18640`. Each server reserves gateway +0, fake
  engine +1 and strict-engine candidates +2 through +9. The entire allocation
  must fit within 18000–18998 or the command fails early. The default range is
  18640–18729. Other fixture tools stay out of it (perf uses 18800–18839;
  `tests/test_port_ranges.py` checks). The fresh-instance
  journeys take the first free ports from 18000 upward.
- The ten-minute bar (release bar 1) has one clock. `fresh-compose` writes the
  Compose bring-up (`docker compose pull`, `build`, `up` until `/health`
  answers) to `raw/compose-bringup.json`; `first-run.spec.ts` writes its browser
  time to `raw/first-run-ui.json`. The step `journey-clock`
  (`scripts/ci/journey_clock.py`) adds them into the `journey` section of
  `cost.json` (`compose_bringup_s`, `first_run_ui_s`, `total_s`, `limit_s`: 600,
  `passed`) and fails at 600 s or more, or when either half is missing;
  `gate_cost.py report` keeps that section when it writes the rest. Cached
  Docker layers make the bring-up a floor, not a ceiling.
- The browser steps share one completed web directory, read-only: no per-lane
  copies and no npm install or build while they run. The runner writes
  (`test-results`, auth files, screenshots, reports) go to the run directory;
  `web/test-results` is a symlink to it. The step `e2e-tree-unchanged` fails
  the run if any other file in `web/` changed.
- Every step writes a private receipt and log. The parent waits for every job,
  aggregates receipts, rejects missing/nonzero/skipped steps, and retains logs
  and case receipts. Browser traces and mutable state are deleted on exit,
  including after failures. Interrupts terminate job process groups
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
| after: 3 pool servers, 9 workers | 480 s | 3,033 | 735 / 0 | 6 |
| VT moves to vitest (426 cases), same layout | 411 s | 2,125 | 426 / 0 | 6 |
| global-state beside the pool on two servers, parallel cases per file, 2 pool servers, 10 workers (default), mean of 3 | 204 s | 1,779 | 426 / 0 | 6 |

Wall seconds stay noisy while other agents use the host (load 15-31 here). The
two serial `@global-state` projects each run about 115 s of test time; the pool
ends at about the same time. At that point `try.spec.ts` was the tail: its cases shared the lane's Try key
and ran one after another, and the file sorted last. The rest of
the gate (Python tier) is unchanged.

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

## Installed dependency cache

The gate keeps successful web `node_modules` and browser Python environments in
`~/Documents/azizam/ci-cache` (`AZIZAM_CI_CACHE` overrides it). This directory is
outside the scratch reaper. Delete its entries while no gate runs to reclaim disk
or force a cold dependency install. Download caches and Docker layers are separate;
a cold installed-cache run does not clear them. The cache keeps the current and
one recent snapshot per kind and protects active keys with locks during eviction.
The current lockfile has dependency install hooks, so Node cache reuse requires
the same Git tree; a new tree installs again. The venv cache is retained as a
requested reusable environment, but measured copies are slower than uv's existing
download-cache install. Both paths stay off the measured critical path.

`scripts/ci/install_cached.py` keys entries on the manifests and lockfile contents,
Node and npm versions or the resolved Python interpreter and uv version, platform,
installer configuration, installer environment, and the helper implementation.
The Python key includes uv configuration files and interpreter build settings.
The npm key includes effective npm configuration and the complete Git tree when
a dependency has an install hook, since hooks can read arbitrary project files.
Root install hooks, workspaces and local dependencies bypass the npm cache because they can read arbitrary source.
Per-call tracing IDs are removed from the installer environment before keying or
execution. Keys contain hashes; configuration and environment values are never logged.

A key lock serializes population. An install failure publishes no completed entry.
Only a complete entry whose file contents, modes and symlink targets match its
stored digest can be reused; a damaged entry is rebuilt. Each run gets an ordinary
private copy
(`cp -a --reflink=auto`, with a full-copy fallback), so test or tool writes cannot
change the cache or another run. Python console launchers are relocatable. The
fresh-instance journey and hermetic pytest still create clean environments from
the hashed lock on every run. Every test still runs, including on cache hits.

Browser Python setup and browser installation start beside the web build and unit
suite after web dependencies are ready. Browsers still wait for all these steps,
Compose and conformance. Compose cannot overlap Chromium because Docker network
changes abort in-flight browser requests. The gate enables the existing two-server
global-state split (`UI_E2E_GLOBAL=2`); each server owns separate state and ports.
The long serial People file starts early in its own one-worker project on the
existing gateway. Its case order stays intact, and it is excluded from the pool
project to avoid duplicate runs. The parallel pool, Try serialization, all three
browser configurations, unchanged build check, journey clock, case aggregation and policy checks remain in the gate.
`--target main` also selects the local `origin/main` budget comparison without
posting signoff. Signoff still requires `--signoff --force` and remote checks.

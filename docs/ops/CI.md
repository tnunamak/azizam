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
- Only one `ci-local` runs at a time on the host: it holds
  `flock ~/.tmp/azizam-ci.lock` for the whole run, and a second run waits
  (the lock file names the last `ci-local` that held it). Do not wrap it in
  `flock` on the same file: the inner lock would wait for the outer one
  forever.
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
- Playwright: `plan_shards.py` places each spec file on a seeded choice among
  the lanes within half the file's estimate of the least-loaded lane, so the
  seed changes which files share a fixture server.

## Case receipts and policy

The `cases` step merges pytest JUnit XML, the vitest JSON report and each
lane's Playwright JSON report into `cases.jsonl`, one line per attempt:
framework, config, project, case ID, file, outcome, attempt index, duration,
seed and SHA (`scripts/ci/merge_cases.py`).

The `policy` step (`scripts/ci/policy.py`) fails the run when:

- a retry exists: a rerun plugin in the hermetic lock, a flaky or rerun
  marker, a `--retries`/`--retry`/`--reruns` flag in the gate scripts, a
  nonzero retry in a web test or test config, a Playwright config the run
  used without `retries: 0`, the vitest config without `retry: 0`, or an
  attempt index above 0;
- a test file ran in no gate step and is not an `[[orphan]]` in
  `tests/suites.toml` with an unexpired `expires_on`;
- a case skipped (or xfailed) without a `[[skip]]` (`[[xfail]]`) entry in
  `tests/suites.toml`;
- a quarantine manifest or marker exists (TEST_STRATEGY.md 3.2);
- a framework's first-attempt case count exceeds `tests/budgets.toml`.

An entry that no longer matches the run (the orphan now runs, the case no
longer skips) also fails, so the lists only shrink. `tests/test_test_policy.py`
injects each violation into a copy of the tree and checks that the policy
fails, and that the clean copy passes.

## Signoff

`scripts/ci-local --force --signoff --target main [--closes-nightly-red N]`
runs on the merge candidate (the target tip with the change merged) and posts
a `signoff` commit status with `gh signoff create`, which needs the commit on
a pushed branch. It refuses with exit 3, and writes no signoff receipt,
unless all of these hold (TEST_STRATEGY.md 1.2):

1. `--force`: every required step runs fresh in this invocation, never from
   the tree cache; `--skip-compose` is refused.
2. `HEAD^1` is the tip of `origin/<target>` (`git ls-remote`), checked before
   the run and again, unchanged, before posting.
3. No open GitHub issue labelled `nightly-red`, except the one number given
   with `--closes-nightly-red`.
4. Every expected step, `policy` included, passed.

The receipt `signoff.tsv` in the run's receipt directory records SHA, tree,
target, target tip, seed, start and finish, the step set, the policy summary,
the bypassed `nightly-red` issue, and the toolchain (both Python
environments, Node, Chromium, ffmpeg). An interrupted or failed run writes no
`signoff.tsv`; a new run deletes the previous one before it starts. The
receipt is written just before `gh signoff create`; if that post fails, the
run exits nonzero and the receipt records a signoff that GitHub lacks.

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
- `--shards N` (default `10`) main Playwright lanes run in parallel with the
  live-data and household lanes. `scripts/e2e/plan_shards.py` assigns whole
  spec files to lanes by the measured times in `web/e2e/shard-weights.tsv`
  (longest first, to the least-loaded lane) and fails unless every listed
  file is in exactly one lane. Each lane runs its list with `--test-list`.
  `--shard i/N` splits by test count: with 12 lanes one lane ran 414 s and
  another 18 s. Workers remain `1` within each fixture and each file keeps its
  test order. Regenerate the weights after large spec changes with
  `scripts/e2e/plan_shards.py weights <receipts>/logs/e2e-shard-*.log`; stale
  weights change balance only, never coverage.
- `--port BASE` defaults to `18640`. Main lane `i` uses `BASE + 10*i`
  (i starts at 1); live-data and household use the next two lanes. Each lane
  reserves gateway +0, fake engine +1 and strict-engine candidates +2 through +9.
  The entire allocation must fit within 18000–18998 or the command fails early.
  The fresh-instance journeys take the first free ports from 18000 upward.
- Each browser lane has its own port-named worktree, state root, results,
  screenshots and auth files. It receives an isolated copy of the completed web
  directory (copy-on-write on supporting filesystems), and shares only the
  completed, read-only browser Python environment. No npm install or build runs
  concurrently against that directory. Legacy live-data auth/manifest names stay
  compatible because their containing worktree is private to that lane.
- Every step writes a private receipt and log. The parent waits for every job,
  aggregates receipts, rejects missing/nonzero/skipped steps, and retains all
  failing-run browser evidence by port. Interrupts terminate job process groups
  before worktrees are removed. `setsid` and GNU-compatible `cp --reflink` are
  required. Extra worktrees and copied dependencies need additional disk space.

## Duration

Measured on 2026-10-11 (24 cores; other agents kept the load average at
10-30). Seconds per step; steps overlap, so the total is the wall clock.

| Step | Before (4 shards, serial Python) | After (10 lanes, 12 workers) |
|---|---|---|
| python-hermetic | 2032 | 262-368 |
| e2e main lanes (slowest) | 523 | 279-307 |
| e2e-household | 97 | 152-170 |
| fresh-compose | 66 (after the lanes) | 48-145 (before the lanes) |
| web-check | 50 | 66-71 |
| **Total** | **2032 (33 min 52 s)** | **421-488** |

The Python suite alone takes 159 s with 20 workers and 216 s with 12. In
the gate it finishes before the slowest browser lane, which ends the run, and
all jobs share the CPU, so more workers would only add contention. The lanes
start after Compose, whose time varies with Docker's build cache.

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

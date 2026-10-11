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
- It writes receipts to `~/.tmp/azizam-ci-receipts/<tree>/`: `steps.tsv`
  (step, exit code, seconds), `logs/`, and browser evidence only when a run
  fails. A tree that already passed is not run again (`--force` reruns it).
- `scripts/ci-local --signoff` posts a `signoff` commit status with
  `gh signoff` when every job passes. The branch must be pushed first.
- The hosted workflow runs only when started by hand
  (`gh workflow run CI --ref <branch>`). It uploads diagnostics only when a
  job fails, keeps them 1 day, and cancels an older run on the same ref.

## Parallel local layout

- Python hermetic tests and the native/Compose fresh-instance job start
  alongside web installation/check/build/unit tests. Each job has a separate
  clean detached worktree, so helper scripts cannot race on Python environments,
  generated files or the web build.
- After web preparation finishes, `--shards N` (default `4`) starts all main
  Playwright shards with `--shard i/N`, plus live-data and household in parallel.
  Workers remain `1` within each fixture; no test filters or assertions change.
- `--port BASE` defaults to `18640`. Main shard `i` uses `BASE + 10*i`
  (i starts at 1); live-data and household use the next two lanes. Each lane
  reserves gateway +0, fake engine +1 and strict-engine candidates +2 through +9.
  The entire allocation must fit within 18000–18998 or the command fails early.
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
- Expected duration is the maximum of Python, fresh-instance, and web preparation
  plus the slowest browser lane, including copying and contention overhead.
  There is no measured speedup yet. Validate a fresh commit with the same Python
  counts and 680 + 4 + 15 browser cases before trusting a new time comparison.
  File-level sharding preserves each file's ordering; unseen cross-file state
  dependencies must be checked by that full run.

## Limits

- A signoff is the owner's attestation that the local run passed. GitHub does
  not verify it, and it cannot be a required check while the repository has
  no branch protection.
- The fresh-instance Compose journey needs Docker networks. If Docker cannot
  allocate one ("Pool overlaps with other one on this address space"), the
  run fails. `--skip-compose` records the step as skipped, and the run then
  cannot pass.

The fresh-instance Compose journey runs only after every browser lane has finished. It creates and removes a Docker network, and Chromium aborts in-flight requests on any host network change (`net::ERR_NETWORK_CHANGED`); overlapping it with the browser lanes made unrelated journeys fail. It still finishes before the Python suite, so it adds no wall time.

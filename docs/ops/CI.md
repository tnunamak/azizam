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

## Limits

- A signoff is the owner's attestation that the local run passed. GitHub does
  not verify it, and it cannot be a required check while the repository has
  no branch protection.
- The fresh-instance Compose journey needs Docker networks. If Docker cannot
  allocate one ("Pool overlaps with other one on this address space"), the
  run fails. `--skip-compose` records the step as skipped, and the run then
  cannot pass.

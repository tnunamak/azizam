#!/usr/bin/env bash
# Pre-commit gate for the web/ Next.js app (docs/design/WEB_UI_STEERING.md,
# "Quality gates"). Runs the full web check (tsc + eslint + react-doctor
# blocking on errors + next build) whenever staged changes touch web/**.
#
# Setup (one-time, from the repo root):
#   ln -sf ../../scripts/pre-commit-web.sh .git/hooks/pre-commit
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"

# Read completely before matching: grep -q must not SIGPIPE git under pipefail.
# Name-status retains both path columns for renames into or out of web/.
staged="$(git diff --cached --name-status --diff-filter=ACMRD)"
if grep -q $'\tweb/' <<< "$staged"; then
  echo "pre-commit: web/** staged — running 'npm run check' in web/"
  (cd "$repo_root/web" && npm run check)
fi

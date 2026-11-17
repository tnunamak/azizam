#!/usr/bin/env bash
# Pre-commit gate for the web/ Next.js app (docs/design/WEB_UI_STEERING.md,
# "Quality gates"). Runs the full web check (tsc + eslint + react-doctor
# blocking on errors + next build) whenever staged changes touch web/**.
#
# Setup (one-time, from the repo root):
#   ln -sf ../../scripts/pre-commit-web.sh .git/hooks/pre-commit
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"

# Complete Git's output before matching and preserve its failure status.
# NUL framing preserves quoted characters and both rename/copy path columns.
staged="$(mktemp)"
trap 'rm -f -- "$staged"' EXIT
if git diff --cached --name-status -z --diff-filter=ACMRD > "$staged"; then
  :
else
  exit "$?"
fi
web_staged=false
# Status fields (A, M, R100, ...) cannot match web/; examine every path field.
while IFS= read -r -d '' field; do
  if [[ "$field" == web/* ]]; then
    web_staged=true
  fi
done < "$staged"
if [[ "$web_staged" == true ]]; then
  echo "pre-commit: web/** staged — running 'npm run check' in web/"
  (cd "$repo_root/web" && npm run check)
fi

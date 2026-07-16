#!/usr/bin/env bash
set -euo pipefail
repo=${1:?repo}; lock=${2:?lock}; requirements=${3:?requirements}; input=${4:?input}
if ! git -C "$repo" diff --quiet || ! git -C "$repo" diff --cached --quiet; then
  echo "dirty tracked tree" >&2; exit 2
fi
if [[ -n $(git -C "$repo" ls-files --others --exclude-standard) ]]; then
  echo "untracked input" >&2; exit 2
fi
[[ -f $lock && -f $requirements && -f $input ]] || { echo "missing lock or manifest" >&2; exit 2; }
manifest_hash=$(sha256sum "$requirements" | awk '{print $1}')
input_hash=$(sha256sum "$input" | awk '{print $1}')
grep -q "^# hermetic-manifest-sha256: $manifest_hash$" "$lock" || { echo "stale requirements hash" >&2; exit 2; }
grep -q "^# hermetic-input-sha256: $input_hash$" "$lock" || { echo "stale input hash" >&2; exit 2; }
grep -q '^# hermetic-manifest-sha256: [0-9a-f]\{64\}$' "$lock" || { echo "malformed lock header" >&2; exit 2; }
grep -q '^# hermetic-input-sha256: [0-9a-f]\{64\}$' "$lock" || { echo "malformed lock header" >&2; exit 2; }

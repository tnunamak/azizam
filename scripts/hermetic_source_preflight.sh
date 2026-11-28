#!/usr/bin/env bash
set -euo pipefail
repo=${1:?repo}; lock=${2:?lock}; requirements=${3:?requirements}; input=${4:?input}
if ! git -C "$repo" diff --quiet || ! git -C "$repo" diff --cached --quiet; then
  echo "dirty tracked tree" >&2; exit 2
fi
if [[ -n $(git -C "$repo" ls-files --others --exclude-standard) ]]; then
  echo "untracked input" >&2; exit 2
fi
for label in lock requirements input; do
  path=${!label}
  [[ ! -L $path ]] || { echo "$label path is a symlink" >&2; exit 2; }
done
[[ -f $lock && -f $requirements && -f $input ]] || { echo "missing lock or manifest" >&2; exit 2; }
repo_resolved=$(realpath -e -- "$repo") || { echo "repository path cannot be resolved" >&2; exit 2; }
for label in lock requirements input; do
  path=${!label}
  resolved=$(realpath -e -- "$path") || { echo "$label path cannot be resolved" >&2; exit 2; }
  case "$resolved" in
    "${repo_resolved%/}/"*) relative=${resolved#"${repo_resolved%/}/"} ;;
    *) echo "$label path is outside repository" >&2; exit 2 ;;
  esac
  git -C "$repo_resolved" --literal-pathspecs ls-files --error-unmatch -- "$relative" >/dev/null 2>&1 ||
    { echo "$label path is not tracked" >&2; exit 2; }
done
read -r manifest_hash _ < <(sha256sum "$requirements")
read -r input_hash _ < <(sha256sum "$input")
manifest_headers=$(grep -c '^# hermetic-manifest-sha256:' "$lock" || true)
input_headers=$(grep -c '^# hermetic-input-sha256:' "$lock" || true)
[[ $manifest_headers -le 1 ]] || { echo "duplicate manifest hash header" >&2; exit 2; }
[[ $input_headers -le 1 ]] || { echo "duplicate input hash header" >&2; exit 2; }
grep -Fxq -- "# hermetic-manifest-sha256: $manifest_hash" "$lock" || { echo "stale requirements hash" >&2; exit 2; }
grep -Fxq -- "# hermetic-input-sha256: $input_hash" "$lock" || { echo "stale input hash" >&2; exit 2; }
grep -q '^# hermetic-manifest-sha256: [0-9a-f]\{64\}$' "$lock" || { echo "malformed lock header" >&2; exit 2; }
grep -q '^# hermetic-input-sha256: [0-9a-f]\{64\}$' "$lock" || { echo "malformed lock header" >&2; exit 2; }

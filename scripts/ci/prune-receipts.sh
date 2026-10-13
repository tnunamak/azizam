#!/usr/bin/env bash
# Keep the gate's receipt directory bounded (TEST_STRATEGY.md 1.3).
# Every receipt keeps its text evidence (run.tsv, steps, cases.jsonl, logs, raw,
# signoff). Heavy browser artifacts (test-results: traces, screenshots, HTML
# report) are kept only for the newest KEEP_ARTIFACTS receipts. Whole receipts
# older than MAX_AGE_DAYS go, then the oldest go until the total is under
# MAX_GIB. The receipt named by --keep (the run that just ended) is never touched.
# Usage: prune-receipts.sh ROOT [--keep DIR]
set -euo pipefail
root=${1:?root}; shift
keep=
if [[ ${1:-} == --keep ]]; then keep=$(cd "${2:?dir}" 2>/dev/null && pwd || true); fi
keep_artifacts=${AZIZAM_RECEIPTS_KEEP_ARTIFACTS:-5}
max_age_days=${AZIZAM_RECEIPTS_MAX_AGE_DAYS:-90}
max_kib=$(( ${AZIZAM_RECEIPTS_MAX_GIB:-20} * 1024 * 1024 ))
[[ -d $root ]] || exit 0
mapfile -t dirs < <(find "$root" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-)
index=0
for dir in "${dirs[@]}"; do
  [[ $dir == "$keep" ]] && { index=$((index + 1)); continue; }
  if (( index >= keep_artifacts )); then rm -rf -- "$dir/test-results"; fi
  index=$((index + 1))
done
now=$(date +%s)
for dir in "${dirs[@]}"; do
  [[ $dir == "$keep" || ! -d $dir ]] && continue
  age_days=$(( (now - $(stat -c %Y -- "$dir")) / 86400 ))
  if (( age_days > max_age_days )); then rm -rf -- "$dir"; fi
done
total=$(du -sk -- "$root" | cut -f1)
for (( i=${#dirs[@]}-1; i>=0 && total>max_kib; i-- )); do
  dir=${dirs[i]}
  [[ $dir == "$keep" || ! -d $dir ]] && continue
  rm -rf -- "$dir"
  total=$(du -sk -- "$root" | cut -f1)
done

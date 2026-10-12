#!/usr/bin/env bash
# Remove pytest tmpfs allocations whose runner is dead.  The runner names each allocation
# azizam-pytest.<runner pid>-<runner start time>.<random>; a SIGKILLed runner cannot run its
# cleanup trap, so the next run sweeps what a dead owner left.  An allocation whose owner is
# alive with the same start time (a concurrent run) is kept, as is any name without an owner.
# Usage: sweep-stale-pytest-tmpfs.sh ROOT
set -euo pipefail
root=${1:?root}
shopt -s nullglob
for dir in "$root"/azizam-pytest.*; do
  [[ -d $dir && -O $dir && ! -L $dir ]] || continue
  name=${dir##*/}
  [[ $name =~ ^azizam-pytest\.([0-9]+)-([0-9]+)\.[A-Za-z0-9]+$ ]] || continue
  pid=${BASH_REMATCH[1]} started=${BASH_REMATCH[2]}
  stat_line=$(cat "/proc/$pid/stat" 2>/dev/null || true)
  if [[ -n $stat_line ]]; then
    fields=(${stat_line##*) })  # fields after "(comm)": state is [0], start time is [19]
    [[ ${fields[19]:-} == "$started" ]] && continue
  fi
  rm -rf -- "$dir"
done

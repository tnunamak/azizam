# Scratch-directory ownership for test harnesses (source this file).
#
# A harness deletes its own scratch directory when it ends (trap on EXIT, HUP, INT,
# TERM). SIGKILL runs no trap, so a detached watcher removes the directory when its
# owner is gone, and every directory also carries a marker naming its owner,
# "<pid>-<start time>", so the next harness start removes the directories whose owner
# is dead (the watcher can die too: a service stop kills its whole cgroup). A reused
# PID has a different start time, so it does not keep a dead owner's directory alive.
#
#   scratch_mark DIR            record the calling shell as DIR's owner
#   scratch_sweep ROOT PREFIX   remove ROOT/PREFIX*/ directories whose owner is dead;
#                               directories without a marker are left alone.
#                               $SCRATCH_KEEP (a path) is never removed.
#   scratch_on_signal           make HUP, INT and TERM run the EXIT trap
#   scratch_reap_on_death DIR.. start a detached watcher that removes DIR.. once the
#                               calling shell has ended, however it ended
scratch_start_time() {
  local stat_line
  stat_line=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
  stat_line=${stat_line##*) }
  # shellcheck disable=SC2206
  local fields=($stat_line)
  printf '%s' "${fields[19]}"
}
scratch_mark() { printf '%s-%s\n' "$$" "$(scratch_start_time $$)" > "$1/.scratch-owner"; }
scratch_owner_alive() {
  local marker=$1/.scratch-owner pid started
  [[ -f $marker ]] || return 0  # no marker: not ours to judge
  IFS=- read -r pid started < "$marker" || return 1
  [[ $pid =~ ^[0-9]+$ ]] || return 1
  [[ $(scratch_start_time "$pid" || true) == "$started" ]]
}
scratch_sweep() {
  local root=$1 prefix=$2 dir
  for dir in "$root/$prefix"*/; do
    dir=${dir%/}
    [[ -d $dir && ! -L $dir && -O $dir ]] || continue
    [[ ${SCRATCH_KEEP:-} != "$dir" ]] || continue
    scratch_owner_alive "$dir" || rm -rf -- "$dir"
  done
  return 0
}
scratch_on_signal() {
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
}
scratch_reap_on_death() {
  local started
  started=$(scratch_start_time $$) || return 0
  # The watcher reads /proc directly (no fork per poll) and holds no terminal or pipe open.
  setsid -f bash -c '
    owner=$1 started=$2; shift 2
    while read -r line 2>/dev/null < "/proc/$owner/stat"; do
      line=${line##*) }; fields=($line)
      [[ ${fields[19]} == "$started" ]] || break
      sleep 2
    done
    rm -rf -- "$@"
  ' scratch-reaper "$$" "$started" "$@" </dev/null >/dev/null 2>&1 || true
}

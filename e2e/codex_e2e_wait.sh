#!/usr/bin/env bash
# Sourced by the smoke runner; importing this file starts no process.

wait_ready() {
  local url=$1 deadline_s=$2
  [[ "$deadline_s" =~ ^[0-9]+$ ]] || return 2
  local end=$((SECONDS + deadline_s)) remaining
  while (( SECONDS < end )); do
    remaining=$((end - SECONDS))
    (( remaining > 2 )) && remaining=2
    (( remaining < 1 )) && remaining=1
    if curl --fail --silent --connect-timeout 1 --max-time "$remaining" "$url" >/dev/null; then
      (( SECONDS < end )) || return 1
      return 0
    fi
    (( SECONDS < end )) || break
    sleep .1
  done
  return 1
}

stop_group() {
  local pid=$1 grace_s=$2
  # Only accept a positive owned group leader, never an empty/zero/negative PID.
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$grace_s" =~ ^[0-9]+$ ]] || return 2
  local end=$((SECONDS + grace_s))
  kill -TERM -- "-$pid" 2>/dev/null || true
  while kill -0 -- "-$pid" 2>/dev/null; do
    (( SECONDS < end )) || break
    sleep .1
  done
  if kill -0 -- "-$pid" 2>/dev/null; then
    kill -KILL -- "-$pid" 2>/dev/null || true
    end=$((SECONDS + 2))
    while kill -0 -- "-$pid" 2>/dev/null; do
      if (( SECONDS >= end )); then
        printf 'Process group %s still alive after KILL\n' "$pid" >&2
        return 1
      fi
      sleep .1
    done
  fi
  # The group is gone; a non-child/reaped PID is harmless.
  wait "$pid" 2>/dev/null || true
}

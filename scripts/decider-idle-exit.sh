#!/usr/bin/env bash
# Run the local decision engine wrapper and exit after DECIDER_IDLE_EXIT_S seconds without a request.
set -u
: "${DECIDER_PORT:?}" "${DECIDER_MODEL:?}" "${LLAMA_SERVER:?}"
idle_limit=${DECIDER_IDLE_EXIT_S:-600}
poll=10
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)
decider_python=${DECIDER_PYTHON:-"$repo_root/venv/bin/python"}

if [[ ${DECIDER_RESIDENCY:-on_demand} == "resident" ]]; then
  cd "$repo_root"
  exec "$decider_python" "$repo_root/engines/decision_engine_server.py"
fi

cd "$repo_root"
"$decider_python" "$repo_root/engines/decision_engine_server.py" &
server=$!
trap 'kill "$server" 2>/dev/null; wait "$server" 2>/dev/null; exit 0' TERM INT

metric() {
  curl -fsS --max-time 3 "http://127.0.0.1:$DECIDER_LLAMA_PORT/metrics" 2>/dev/null |
    awk -v n="llamacpp:$1" '$1 == n {print $2; exit}'
}

last=""
idle=0
while kill -0 "$server" 2>/dev/null; do
  sleep "$poll" &
  wait $!
  tokens=$(metric prompt_tokens_total)
  busy=$(metric requests_processing)
  if [[ -z $tokens ]]; then
    continue
  fi
  if [[ $tokens != "$last" || ${busy:-0} != 0 ]]; then
    last=$tokens
    idle=0
  else
    idle=$((idle + poll))
    if ((idle >= idle_limit)); then
      echo "decider idle for ${idle}s: exiting" >&2
      kill "$server" 2>/dev/null
      wait "$server" 2>/dev/null
      exit 0
    fi
  fi
done
wait "$server"

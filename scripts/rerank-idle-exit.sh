#!/usr/bin/env bash
# Run `llama-server --rerank` and exit 0 after RERANK_IDLE_EXIT_S seconds without a request.
#
# llama-server can free its model after idle time but its process stays. This wrapper ends the
# whole process, so an idle reranker holds no RAM at all. The gateway starts the unit again on
# the next request (lifecycle driver `systemd`, `ensure_ready`).
#
# Environment (all set by rerank-cpu.service):
#   LLAMA_SERVER         path to llama-server
#   RERANK_MODEL         path to the reranker GGUF
#   RERANK_PORT          listen port (127.0.0.1 only)
#   RERANK_THREADS       CPU threads (default 8)
#   RERANK_CTX           context tokens per request (default 4096: the longest query plus document the engine accepts)
#   RERANK_IDLE_EXIT_S   idle seconds before exit (default 600)
set -u
: "${LLAMA_SERVER:?}" "${RERANK_MODEL:?}" "${RERANK_PORT:?}"
threads=${RERANK_THREADS:-8}
ctx=${RERANK_CTX:-4096}
idle_limit=${RERANK_IDLE_EXIT_S:-600}
poll=10

# --device none: keep the compute buffers off every GPU, not only the weights.
"$LLAMA_SERVER" -m "$RERANK_MODEL" --rerank --host 127.0.0.1 --port "$RERANK_PORT" \
  -ngl 0 --device none -t "$threads" -c "$ctx" -b "$ctx" -ub "$ctx" -np 1 --metrics --no-webui &
server=$!
trap 'kill "$server" 2>/dev/null; wait "$server" 2>/dev/null; exit 0' TERM INT

metric() { # metric <name>: value of a llama-server Prometheus gauge or counter, empty if unreadable
  curl -fsS --max-time 3 "http://127.0.0.1:$RERANK_PORT/metrics" 2>/dev/null |
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
    continue  # still loading: not idle
  fi
  if [[ $tokens != "$last" || ${busy:-0} != 0 ]]; then
    last=$tokens
    idle=0
  else
    idle=$((idle + poll))
    if ((idle >= idle_limit)); then
      echo "rerank idle for ${idle}s: exiting" >&2
      kill "$server" 2>/dev/null
      wait "$server" 2>/dev/null
      exit 0
    fi
  fi
done
wait "$server"

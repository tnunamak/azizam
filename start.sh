#!/bin/bash
# Vivid Fish AI Gateway service starter
# Unified local AI API for TabbyAPI/KoboldCpp/TTS/STT/ComfyUI backends
# Served externally at ai.vivid.fish (via Traefik on 192.168.1.4)

cd "$(dirname "${BASH_SOURCE[0]}")"

# Operator-local machine paths (gitignored; absent on a fresh install, where
# the affected integrations are simply off). See CONFIG.md for the AGW_* vars.
if [ -f ".env.local" ]; then
    set -a
    # shellcheck disable=SC1091
    source ".env.local"
    set +a
fi

# Use proxy's own isolated venv
if [ ! -d "venv" ]; then
    echo "Creating venv..."
    python3 -m venv venv
fi

source venv/bin/activate

REQ_HASH="$(sha256sum requirements.txt | awk '{print $1}')"
REQ_STAMP="venv/.requirements.sha256"
if [ ! -f "$REQ_STAMP" ] || [ "$(cat "$REQ_STAMP")" != "$REQ_HASH" ]; then
    python3 -m pip install -r requirements.txt
    echo "$REQ_HASH" > "$REQ_STAMP"
fi

# Config via environment (override proxy_config.yml defaults)
export OPENAI_PROXY_PORT="${OPENAI_PROXY_PORT:-5000}"
export TABBY_IDLE_TIMEOUT="${TABBY_IDLE_TIMEOUT:-300}"
export TTS_ENABLED="${TTS_ENABLED:-true}"
export TTS_HOST="${TTS_HOST:-http://127.0.0.1:8001}"
export AI_GATEWAY_LITELLM_BASE_URL="${AI_GATEWAY_LITELLM_BASE_URL:-http://127.0.0.1:4011}"
export AI_GATEWAY_LITELLM_API_KEY="${AI_GATEWAY_LITELLM_API_KEY:-$(python3 - <<'PY'
import secrets
print('sk-local-' + secrets.token_urlsafe(24))
PY
)}"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-$AI_GATEWAY_LITELLM_API_KEY}"
export AI_GATEWAY_INTERNAL_LITELLM_KEY="${AI_GATEWAY_INTERNAL_LITELLM_KEY:-$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)}"

if [ "${AI_GATEWAY_MANAGE_LITELLM:-true}" != "false" ]; then
    LITELLM_CONFIG_PATH="${AI_GATEWAY_LITELLM_CONFIG_PATH:-data/litellm.config.yaml}"
    mkdir -p "$(dirname "$LITELLM_CONFIG_PATH")"
    LITELLM_PID_FILE="${AI_GATEWAY_LITELLM_PID_FILE:-data/litellm.pid}"
    LITELLM_PID=""

    start_litellm() {
        litellm --config "$LITELLM_CONFIG_PATH" --host 127.0.0.1 --port 4011 &
        LITELLM_PID=$!
        echo "$LITELLM_PID" > "$LITELLM_PID_FILE"
    }

    watch_litellm_config() {
        trap 'if [ -n "$LITELLM_PID" ]; then kill "$LITELLM_PID" >/dev/null 2>&1 || true; fi; exit 0' EXIT INT TERM
        LAST_HASH=""
        while true; do
            while [ ! -s "$LITELLM_CONFIG_PATH" ]; do sleep 1; done
            NEXT_HASH="$(sha256sum "$LITELLM_CONFIG_PATH" | awk '{print $1}')"
            if [ "$NEXT_HASH" != "$LAST_HASH" ] || ! kill -0 "${LITELLM_PID:-0}" >/dev/null 2>&1; then
                if [ -n "$LITELLM_PID" ]; then
                    kill "$LITELLM_PID" >/dev/null 2>&1 || true
                    wait "$LITELLM_PID" >/dev/null 2>&1 || true
                fi
                start_litellm
                LAST_HASH="$NEXT_HASH"
            fi
            sleep "${AI_GATEWAY_LITELLM_RELOAD_INTERVAL:-2}"
        done
    }

    watch_litellm_config &
    LITELLM_WATCHER_PID=$!

    cleanup() {
        kill "$LITELLM_WATCHER_PID" >/dev/null 2>&1 || true
        if [ -s "$LITELLM_PID_FILE" ]; then
            kill "$(cat "$LITELLM_PID_FILE")" >/dev/null 2>&1 || true
        fi
    }
    trap cleanup EXIT INT TERM

    python3 proxy.py "$@" &
    PROXY_PID=$!
    wait "$PROXY_PID"
    exit $?
fi

exec python3 proxy.py "$@"

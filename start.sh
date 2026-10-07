#!/bin/bash
# Azizam service starter
# Unified local AI API for TabbyAPI/KoboldCpp/TTS/STT/ComfyUI backends
# Served externally at ai.vivid.fish (via Traefik on 192.168.1.4)

cd "$(dirname "${BASH_SOURCE[0]}")"

# Operator-local machine paths (gitignored; absent on a fresh install, where
# the affected integrations are simply off). See CONFIG.md for the AZIZAM_* vars.
if [ -f ".env.local" ]; then
    set -a
    # shellcheck disable=SC1091
    source ".env.local"
    set +a
fi

# Use proxy's own isolated venv
if [ ! -d "venv" ]; then
    echo "Creating venv..."
    uv venv venv --python 3.12
fi


source venv/bin/activate

REQ_FILE="${AZIZAM_REQUIREMENTS_FILE:-requirements.txt}"
REQ_HASH="$(sha256sum "$REQ_FILE" | awk '{print $1}')"
REQ_STAMP="venv/.requirements.sha256"
if [ ! -f "$REQ_STAMP" ] || [ "$(cat "$REQ_STAMP")" != "$REQ_HASH" ]; then
    if [ "${AZIZAM_REQUIREMENTS_HASHES:-false}" = "true" ]; then
        uv pip install --python ./venv/bin/python --require-hashes -r "$REQ_FILE"
    else
        uv pip install --python ./venv/bin/python -r "$REQ_FILE"
    fi
    echo "$REQ_HASH" > "$REQ_STAMP"
fi

# Config via environment (override proxy_config.yml defaults)
export AZIZAM_PORT="${AZIZAM_PORT:-5000}"
export TABBY_IDLE_TIMEOUT="${TABBY_IDLE_TIMEOUT:-300}"
export TTS_ENABLED="${TTS_ENABLED:-true}"
export TTS_HOST="${TTS_HOST:-http://127.0.0.1:8001}"
export AZIZAM_LITELLM_BASE_URL="${AZIZAM_LITELLM_BASE_URL:-http://127.0.0.1:4011}"
export AZIZAM_LITELLM_API_KEY="${AZIZAM_LITELLM_API_KEY:-$(python3 - <<'PY'
import secrets
print('sk-local-' + secrets.token_urlsafe(24))
PY
)}"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-$AZIZAM_LITELLM_API_KEY}"
export AZIZAM_INTERNAL_LITELLM_KEY="${AZIZAM_INTERNAL_LITELLM_KEY:-$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)}"

if [ "${AZIZAM_MANAGE_LITELLM:-true}" != "false" ]; then
    # LiteLLM's virtual-key DB path shells out to the Prisma CLI when
    # DATABASE_URL is set. In systemd, nvm-managed node is not on PATH, so
    # prisma-client-py falls back to ~/.cache/prisma-python/nodeenv. If a prior
    # nodeenv install was interrupted, Prisma raises "Target `bin` directory
    # does not exist" and LiteLLM may continue with an unmigrated schema. Repair
    # that exact broken cache and fail fast if Prisma still cannot start.
    if [ -n "${DATABASE_URL:-}" ]; then
        PRISMA_NODEENV_DIR="${PRISMA_NODEENV_DIR:-$HOME/.cache/prisma-python/nodeenv}"
        if ! command -v node >/dev/null 2>&1 \
            && [ -d "$PRISMA_NODEENV_DIR" ] \
            && [ ! -x "$PRISMA_NODEENV_DIR/bin/node" ]; then
            echo "[start] Removing incomplete Prisma nodeenv cache: $PRISMA_NODEENV_DIR" >&2
            rm -rf "$PRISMA_NODEENV_DIR"
        fi
        if ! timeout "${AZIZAM_PRISMA_PREFLIGHT_TIMEOUT:-90}" python3 -m prisma --version >/dev/null; then
            echo "[start] Prisma CLI preflight failed; refusing to start LiteLLM with an unchecked DB schema." >&2
            exit 1
        fi
    fi

    # LiteLLM imports the generated Prisma client even without DATABASE_URL.
    # A requirements install does not generate it. Regenerate after a schema
    # or requirements change, and repair a missing client on an existing venv.
    PRISMA_SCHEMA="$(python3 -c 'from importlib.util import find_spec; from pathlib import Path; print(Path(next(iter(find_spec("litellm").submodule_search_locations))) / "proxy" / "schema.prisma")')"
    if [ ! -f "$PRISMA_SCHEMA" ]; then
        echo "[start] LiteLLM Prisma schema is missing; refusing to start." >&2
        exit 1
    fi
    PRISMA_HASH="$(sha256sum "$PRISMA_SCHEMA" | awk '{print $1}'):$REQ_HASH"
    PRISMA_STAMP="venv/.prisma-generated.sha256"
    if [ ! -f "$PRISMA_STAMP" ] || [ "$(cat "$PRISMA_STAMP")" != "$PRISMA_HASH" ] \
        || ! python3 -c 'from prisma import Prisma' >/dev/null 2>&1; then
        if ! timeout "${AZIZAM_PRISMA_PREFLIGHT_TIMEOUT:-90}" python3 -m prisma generate --schema "$PRISMA_SCHEMA"; then
            echo "[start] Prisma client generation failed; refusing to start LiteLLM." >&2
            exit 1
        fi
        echo "$PRISMA_HASH" > "$PRISMA_STAMP"
    fi

    LITELLM_CONFIG_PATH="${AZIZAM_LITELLM_CONFIG_PATH:-data/litellm.config.yaml}"
    AZIZAM_ADMIN_DB_PATH="${AZIZAM_ADMIN_DB:-data/admin.db}"
    mkdir -p "$(dirname "$LITELLM_CONFIG_PATH")"
    LITELLM_PID_FILE="${AZIZAM_LITELLM_PID_FILE:-data/litellm.pid}"
    LITELLM_PID=""

    start_litellm() {
        python3 -m gateway.provider_sidecar run --db "$AZIZAM_ADMIN_DB_PATH" --config "$LITELLM_CONFIG_PATH" -- \
            litellm --config "$LITELLM_CONFIG_PATH" --host 127.0.0.1 --port 4011 &
        LITELLM_PID=$!
        echo "$LITELLM_PID" > "$LITELLM_PID_FILE"
    }

    litellm_runtime_hash() {
        {
            sha256sum "$LITELLM_CONFIG_PATH" | awk '{print $1}'
            python3 -m gateway.provider_sidecar fingerprint --db "$AZIZAM_ADMIN_DB_PATH"
        } | sha256sum | awk '{print $1}'
    }

    watch_litellm_config() {
        trap 'if [ -n "$LITELLM_PID" ]; then kill "$LITELLM_PID" >/dev/null 2>&1 || true; fi; exit 0' EXIT INT TERM
        LAST_HASH=""
        while true; do
            while [ ! -s "$LITELLM_CONFIG_PATH" ]; do sleep 1; done
            NEXT_HASH="$(litellm_runtime_hash)"
            if [ "$NEXT_HASH" != "$LAST_HASH" ] || ! kill -0 "${LITELLM_PID:-0}" >/dev/null 2>&1; then
                if [ -n "$LITELLM_PID" ]; then
                    kill "$LITELLM_PID" >/dev/null 2>&1 || true
                    wait "$LITELLM_PID" >/dev/null 2>&1 || true
                fi
                start_litellm
                LAST_HASH="$NEXT_HASH"
            fi
            sleep "${AZIZAM_LITELLM_RELOAD_INTERVAL:-2}"
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
    trap cleanup EXIT

    # Graceful stop: systemd (KillMode=mixed) sends SIGTERM to this script only.
    # Forward it to the proxy and wait for uvicorn to finish in-flight
    # responses BEFORE stopping LiteLLM, which chat requests may still depend
    # on. systemd's TimeoutStopSec bounds the wait. See docs/ops/SAFE_RESTART.md.
    PROXY_PID=""
    stop_proxy() {
        if [ -n "$PROXY_PID" ] && kill -0 "$PROXY_PID" >/dev/null 2>&1; then
            kill -TERM "$PROXY_PID" >/dev/null 2>&1 || true
            wait "$PROXY_PID" >/dev/null 2>&1 || true
        fi
        exit 0
    }
    trap stop_proxy INT TERM

    python3 proxy.py "$@" &
    PROXY_PID=$!
    wait "$PROXY_PID"
    exit $?
fi

exec python3 proxy.py "$@"

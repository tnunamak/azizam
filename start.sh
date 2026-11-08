#!/bin/bash
# Azizam service starter
# Unified local AI API for local LLM/TTS/STT/ComfyUI backends

SCRIPT_PATH="$(realpath -- "${BASH_SOURCE[0]}")"
cd "$(dirname "$SCRIPT_PATH")"

# Operator-local machine paths (gitignored; absent on a fresh install, where
# the affected integrations are simply off). See docs/CONFIG.md for the AZIZAM_* vars.
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
REQ_HASH="$(sha256sum "$REQ_FILE" | cut -d' ' -f1)"
REQ_STAMP="venv/.requirements.sha256"
if [ ! -f "$REQ_STAMP" ] || [ "$(cat "$REQ_STAMP")" != "$REQ_HASH" ]; then
    if [ "${AZIZAM_REQUIREMENTS_HASHES:-false}" = "true" ]; then
        uv pip install --python ./venv/bin/python --require-hashes -r "$REQ_FILE"
    else
        uv pip install --python ./venv/bin/python -r "$REQ_FILE"
    fi
    echo "$REQ_HASH" > "$REQ_STAMP"
fi

# Sweep under the lifetime lock before any child starts. The descriptor survives exec into this shell
# and the proxy; a second launch cannot delete a running gateway's scratch files.
# The exec helper also exports TMPDIR, TMP and TEMP.
if [ -z "${AZIZAM_PRIVATE_TMPDIR_LOCK_FD:-}" ]; then
    exec python3 -m gateway.private_tmp exec /bin/bash "$SCRIPT_PATH" "$@"
fi

# Config via environment (override proxy_config.yml defaults)
export AZIZAM_PORT="${AZIZAM_PORT:-5000}"
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

export AZIZAM_MANAGE_LITELLM="${AZIZAM_MANAGE_LITELLM:-true}"
export AZIZAM_LITELLM_PID_FILE="${AZIZAM_LITELLM_PID_FILE:-data/litellm.pid}"
if [ "$AZIZAM_MANAGE_LITELLM" != "false" ]; then
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
    PRISMA_HASH="$(sha256sum "$PRISMA_SCHEMA" | cut -d' ' -f1):$REQ_HASH"
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
    AZIZAM_ADMIN_DB_PATH="$(python3 -m gateway.provider_sidecar db-path)" || exit 1
    mkdir -p "$(dirname "$LITELLM_CONFIG_PATH")"
    LITELLM_PID_FILE="${AZIZAM_LITELLM_PID_FILE:-data/litellm.pid}"
    LITELLM_RUNTIME_HASH_FILE="${LITELLM_PID_FILE}.runtime-hash"
    LITELLM_PID=""

    start_litellm() {
        local receipt_timeout=${AZIZAM_LITELLM_RECEIPT_TIMEOUT_S:-120} receipt_started
        case $receipt_timeout in
            ''|*[!0-9]*|0*|?????????*)
                echo "[start] AZIZAM_LITELLM_RECEIPT_TIMEOUT_S must be a whole number from 1 to 99999999; using 120." >&2
                receipt_timeout=120;;
        esac
        rm -f "$LITELLM_RUNTIME_HASH_FILE"
        if [ -n "${LITELLM_LAUNCH_CONFIG_PATH:-}" ]; then
            rm -f "$LITELLM_LAUNCH_CONFIG_PATH"
        fi
        LITELLM_LAUNCH_CONFIG_PATH="$(mktemp "${LITELLM_CONFIG_PATH}.launch.XXXXXXXX")" || return 1
        python3 -m gateway.provider_sidecar run --db "$AZIZAM_ADMIN_DB_PATH" --config "$LITELLM_CONFIG_PATH" \
            --launch-config "$LITELLM_LAUNCH_CONFIG_PATH" --runtime-hash-file "$LITELLM_RUNTIME_HASH_FILE" -- \
            litellm --config "$LITELLM_LAUNCH_CONFIG_PATH" --host 127.0.0.1 --port 4011 &
        LITELLM_PID=$!
        receipt_started=$SECONDS
        echo "$LITELLM_PID" > "$LITELLM_PID_FILE"
        while [ ! -s "$LITELLM_RUNTIME_HASH_FILE" ]; do
            if ! kill -0 "$LITELLM_PID" >/dev/null 2>&1; then
                wait "$LITELLM_PID" >/dev/null 2>&1 || true
                echo "[start] LiteLLM sidecar failed before acknowledging its registry snapshot." >&2
                return 1
            fi
            if [ $((SECONDS - receipt_started)) -ge "$receipt_timeout" ]; then
                # A child ignoring TERM must not hold this watcher indefinitely.
                kill -KILL "$LITELLM_PID" >/dev/null 2>&1 || true
                wait "$LITELLM_PID" >/dev/null 2>&1 || true
                echo "[start] LiteLLM sidecar timed out before acknowledging its registry snapshot." >&2
                return 1
            fi
            sleep 0.1
        done
    }

    litellm_runtime_hash() {
        local config_hash fingerprint runtime_hash
        config_hash="$(sha256sum < "$LITELLM_CONFIG_PATH")" || return 1
        config_hash=${config_hash%% *}
        fingerprint="$(python3 -m gateway.provider_sidecar fingerprint --db "$AZIZAM_ADMIN_DB_PATH")" || return 1
        [[ $config_hash =~ ^[0-9a-f]{64}$ && $fingerprint =~ ^[0-9a-f]{64}$ ]] || return 1
        # Exactly the receipt format: two lowercase hex digests, each with a newline.
        runtime_hash="$(printf '%s\n%s\n' "$config_hash" "$fingerprint" | sha256sum)" || return 1
        runtime_hash=${runtime_hash%% *}
        [[ $runtime_hash =~ ^[0-9a-f]{64}$ ]] || return 1
        printf '%s\n' "$runtime_hash"
    }

    # Each hash starts a Python that spends about 0.7 s of CPU on imports, so
    # the watcher hashes again only when an input file changed. A write in the
    # same clock tick as a stat can keep its mtime; an input changed in the
    # last 2 s is not trusted, and every AZIZAM_LITELLM_HASH_REFRESH_POLLS polls
    # hash again for changes that no file stat shows.
    litellm_runtime_inputs() {
        stat -L -c '%n %d %i %s %.9Y' -- "$LITELLM_CONFIG_PATH" "$AZIZAM_ADMIN_DB_PATH" \
            "$AZIZAM_ADMIN_DB_PATH-wal" "$AZIZAM_ADMIN_DB_PATH-journal" 2>/dev/null
    }

    litellm_runtime_inputs_settled() {
        local now newest
        printf -v now '%(%s)T' -1
        newest="$(stat -L -c '%Y' -- "$LITELLM_CONFIG_PATH" "$AZIZAM_ADMIN_DB_PATH" \
            "$AZIZAM_ADMIN_DB_PATH-wal" "$AZIZAM_ADMIN_DB_PATH-journal" 2>/dev/null | sort -n | tail -1)"
        [ -n "$newest" ] && [ $((now - newest)) -ge 2 ]
    }

    watch_litellm_config() {
        # Remove the launch file first: LiteLLM read it at start-up, and systemd may SIGKILL us while we wait for it.
        trap 'rm -f "$LITELLM_RUNTIME_HASH_FILE" "${LITELLM_LAUNCH_CONFIG_PATH:-}"; if [ -n "$LITELLM_PID" ]; then kill "$LITELLM_PID" >/dev/null 2>&1 || true; wait "$LITELLM_PID" >/dev/null 2>&1 || true; fi; exit 0' EXIT INT TERM
        # Launch files left by earlier runs (killed before their trap ran); mktemp makes exactly 8 suffix characters.
        rm -f "${LITELLM_CONFIG_PATH}".launch.????????
        LAST_HASH=""
        LAST_INPUTS=""
        HASH_POLLS=0
        HASH_UNAVAILABLE=""
        HASH_REFRESH_POLLS=${AZIZAM_LITELLM_HASH_REFRESH_POLLS:-60}
        case $HASH_REFRESH_POLLS in
            ''|*[!0-9]*|0*|?????????*)
                echo "[start] AZIZAM_LITELLM_HASH_REFRESH_POLLS must be a whole number from 1 to 99999999; using 60." >&2
                HASH_REFRESH_POLLS=60;;
        esac
        while true; do
            while [ ! -s "$LITELLM_CONFIG_PATH" ]; do sleep 1; done
            # Read the inputs before the hash: a change after this read shows next poll.
            # stat fails on the optional -wal and -journal files when they are absent but still prints the
            # others; that partial output is the signature, so its status must not stop the watcher.
            INPUTS="$(litellm_runtime_inputs)" || :
            HASH_POLLS=$((HASH_POLLS + 1))
            if [ -z "$LAST_HASH" ] || [ "$INPUTS" != "$LAST_INPUTS" ] \
                || [ "$HASH_POLLS" -ge "$HASH_REFRESH_POLLS" ] \
                || ! litellm_runtime_inputs_settled; then
                # Show the first failure's error; keep a long outage from filling the log.
                if [ -n "$HASH_UNAVAILABLE" ]; then
                    NEXT_HASH="$(litellm_runtime_hash 2>/dev/null)" || NEXT_HASH=""
                else
                    NEXT_HASH="$(litellm_runtime_hash)" || NEXT_HASH=""
                fi
                if [ -z "$NEXT_HASH" ]; then
                    # An unreadable input is not a new runtime: keep the child, but
                    # still restart it below if it died, and retry the hash next poll.
                    [ -n "$HASH_UNAVAILABLE" ] || echo "[start] LiteLLM runtime hash unavailable; keeping the running child and retrying next poll." >&2
                    HASH_UNAVAILABLE=1
                    NEXT_HASH=$LAST_HASH
                    HASH_POLLS=$HASH_REFRESH_POLLS
                else
                    [ -z "$HASH_UNAVAILABLE" ] || echo "[start] LiteLLM runtime hash is available again." >&2
                    HASH_UNAVAILABLE=""
                    HASH_POLLS=0
                fi
            else
                NEXT_HASH=$LAST_HASH
            fi
            LAST_INPUTS=$INPUTS
            # No tracked child always means a launch, even while the hash is unavailable.
            if [ -z "$LITELLM_PID" ] || [ "$NEXT_HASH" != "$LAST_HASH" ] || ! kill -0 "$LITELLM_PID" >/dev/null 2>&1; then
                if [ -n "$LITELLM_PID" ]; then
                    kill "$LITELLM_PID" >/dev/null 2>&1 || true
                    wait "$LITELLM_PID" >/dev/null 2>&1 || true
                fi
                if start_litellm; then
                    LAST_HASH="$(cat "$LITELLM_RUNTIME_HASH_FILE")"
                else
                    # Every failed start has reaped its child; a stale PID could be reused by another process.
                    LAST_HASH=""
                    LITELLM_PID=""
                fi
            fi
            sleep "${AZIZAM_LITELLM_RELOAD_INTERVAL:-2}"
        done
    }

    # The watcher and LiteLLM use no scratch files of ours; a LiteLLM that outlived us must not hold the lock.
    watch_litellm_config {AZIZAM_PRIVATE_TMPDIR_LOCK_FD}>&- &
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

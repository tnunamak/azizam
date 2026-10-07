#!/usr/bin/env bash

# fresh_instance_harness.sh -- spin up an isolated Azizam instance for fresh-instance testing.
#
# WHAT THIS DOES
#   1. Creates a temp directory under /tmp for all persistent stores and config.
#   2. Writes a minimal proxy_config.yml into the temp dir; points AZIZAM_CONFIG_PATH at it.
#   3. Sets env overrides for every persistent store so nothing writes to the repo data/ dir.
#   4. Binds to HARNESS_PORT (default 15000) -- never the live port 5000.
#   5. Fails loudly if startup output references the repo data/ directory.
#
# SOURCES OF TRUTH (documented explicitly)
#   Env: AZIZAM_ADMIN_DB        -- temp admin DB (never touches live data/admin.db)
#   Env: AZIZAM_METRICS_DB      -- temp metrics DB
#   Env: AZIZAM_RESPONSES_DB    -- temp responses DB
#   Env: AZIZAM_RECEIPTS_DB     -- temp receipt store DB
#   Env: AZIZAM_MEDIA_JOBS_DB   -- temp media jobs DB
#   Env: AZIZAM_CONFIG_PATH     -- temp proxy_config.yml written by this script
#   Env: AZIZAM_PORT          -- set to HARNESS_PORT
#   Env: AZIZAM_ADMIN_SESSION_KEY -- fixed test-only secret
#
# USAGE
#   ./scripts/fresh_instance_harness.sh [--smoke] [--fixture] [--browser] [--scenario NAME] [--port PORT]
#
#   --smoke   Run smoke checks (health + admin endpoints) then shut down.
#   --port N  Use port N instead of 15000.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HARNESS_PORT="${HARNESS_PORT:-15000}"
SMOKE_MODE=0
FIXTURE_MODE=0
FIXTURE_SCENARIO="all_clear"
FIXTURE_BROWSER=0

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --smoke) SMOKE_MODE=1; shift ;;
    --fixture) FIXTURE_MODE=1; shift ;;
    --browser) FIXTURE_BROWSER=1; shift ;;
    --scenario) FIXTURE_SCENARIO="$2"; shift 2 ;;
    --port)  HARNESS_PORT="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ "$FIXTURE_MODE" == 1 ]]; then
  case "$FIXTURE_SCENARIO" in
    all_clear|required_down|degraded|status_unavailable|standby_stopped|history_dense) ;;
    *) echo "ERROR: unsupported fixture scenario: $FIXTURE_SCENARIO" >&2; exit 1 ;;
  esac
  if [[ "$HARNESS_PORT" != 15000 && "$HARNESS_PORT" != 0 ]]; then
    echo "ERROR: fixture mode uses its retained loopback ephemeral listener; pass --port 0" >&2
    exit 1
  fi
  HARNESS_PORT=0
fi
if [[ "$FIXTURE_BROWSER" == 1 && "$FIXTURE_MODE" != 1 ]]; then
  echo "ERROR: --browser requires --fixture" >&2
  exit 1
fi

# -- Safety guard --------------------------------------------------------------
LIVE_DB="$REPO_ROOT/data/admin.db"
for env_name in AZIZAM_ADMIN_DB; do
  if [[ "${!env_name:-}" == "$LIVE_DB" || "${!env_name:-}" == "$(dirname "$REPO_ROOT")/azizam/data/admin.db" ]]; then
    echo "ERROR: $env_name is set to a production DB path. Aborting." >&2
    exit 1
  fi
done
for env_name in AZIZAM_PORT HARNESS_PORT; do
  if [[ "${!env_name:-}" == "5000" ]]; then
    echo "ERROR: Refusing to bind on live port 5000." >&2
    exit 1
  fi
done

# -- Temp workspace -------------------------------------------------------------
if [[ "$FIXTURE_MODE" == 1 ]]; then
  mkdir -p "$HOME/.tmp"
  TMPDIR_ROOT=$(mktemp -d "$HOME/.tmp/gw-ux-p1b-XXXXXX")
else
  TMPDIR_ROOT=$(mktemp -d /tmp/azizam-fresh-XXXXXX)
fi

cleanup_fixture() {
  if [[ "$FIXTURE_MODE" == 1 && -n "${PROXY_PID:-}" ]]; then
    local pgid
    pgid=$(ps -o pgid= -p "$PROXY_PID" 2>/dev/null | tr -d ' ' || true)
    if [[ -n "$pgid" ]]; then
      kill -TERM -- "-$pgid" 2>/dev/null || true
      for _ in $(seq 1 20); do kill -0 -- "-$pgid" 2>/dev/null || break; sleep 0.1; done
      kill -KILL -- "-$pgid" 2>/dev/null || true
      wait "$PROXY_PID" 2>/dev/null || true
      if kill -0 -- "-$pgid" 2>/dev/null; then echo "ERROR: fixture process group survived cleanup" >&2; return; fi
    fi
  fi
  if [[ "$TMPDIR_ROOT" == "$HOME/.tmp/gw-ux-p1b-"* || "$TMPDIR_ROOT" == /tmp/azizam-fresh-* ]]; then
    echo "[harness] Cleaning up $TMPDIR_ROOT"
    rm -rf "$TMPDIR_ROOT"
  fi
}
trap cleanup_fixture EXIT

echo "[harness] Temp workspace: $TMPDIR_ROOT"
echo "[harness] Port:           $HARNESS_PORT"
echo "[harness] Admin DB:       $TMPDIR_ROOT/admin.db"

# -- Minimal proxy_config.yml --------------------------------------------------
# Generic backends pointing at localhost ports that won't be up -- the gateway
# starts fine and reports them as unreachable. This exercises the bootstrap
# path without any Tim-specific service details.
cat > "$TMPDIR_ROOT/proxy_config.yml" <<'YAML'
proxy:
  host: 127.0.0.1
  port: 15000  # overridden by AZIZAM_PORT env below

# No auth required for fresh-instance smoke: lets health + admin endpoints work
# without needing to provision an App credential first.
auth:
  enabled: false

admin:
  session_signing_key: harness-test-session-key-not-for-production
  oidc:
    # The harness binds plain http://127.0.0.1, but the session cookie defaults
    # to Secure — and browsers refuse to store Secure cookies on insecure
    # origins, so sign-in would "just reset the page". Turn Secure off for local
    # http testing. (Real deployments serve https and keep the secure default.)
    cookie:
      secure: false

backends:
  # tabby entry must be present with managed: false to prevent the gateway
  # from trying to spawn a TabbyAPI subprocess (TABBY_MANAGED defaults to true
  # when the key is absent). The host/port are placeholders; the backend will
  # be unreachable but the gateway starts and serves all non-LLM routes.
  tabby:
    name: TabbyAPI (harness placeholder -- not started)
    native_passthrough: true
    host: 127.0.0.1
    port: 5050
    health_endpoint: /v1/models
    role: standby
    managed: false
    capabilities:
      - chat
      - completions

  example-llm:
    name: Example LLM backend (harness placeholder)
    native_passthrough: true
    host: 127.0.0.1
    port: 19999
    health_endpoint: /v1/models
    role: standby
    capabilities:
      - chat
      - completions
    managed: false

routing:
  default_backend: example-llm

roles:
  default:
    backend: example-llm
YAML

if [[ "$FIXTURE_MODE" == 1 ]]; then
cat > "$TMPDIR_ROOT/proxy_config.yml" <<YAML
proxy:
  host: 127.0.0.1
  port: 0
auth:
  enabled: true
admin:
  session_signing_key_env: AZIZAM_ADMIN_SESSION_KEY
  oidc:
    enabled: false
    cookie:
      secure: false
  user_defaults:
    self_service_apps: true
    auto_create_personal_app: false
backends:
  fixture-required:
    name: Fixture required service
    host: 127.0.0.1
    port: 9
    role: required
    managed: false
    native_passthrough: true
    capabilities: [chat]
    models:
      fixture-model: fixture-model
  fixture-standby:
    name: Fixture standby service
    host: 127.0.0.1
    port: 9
    role: standby
    managed: false
    native_passthrough: true
    capabilities: [chat]
    models:
      fixture-standby-model: fixture-standby-model
  comfyui:
    enabled: false
  stt:
    enabled: false
  tts:
    enabled: false
routing:
  default_backend: fixture-required
roles:
  default:
    backend: fixture-required
images:
  cache_dir: $TMPDIR_ROOT/images
  vram_estimates_file: $TMPDIR_ROOT/vram_estimates.json
  media_jobs:
    payload_dir: $TMPDIR_ROOT/media_payloads
    resume_on_start: false
  background_removal:
    enabled: false
YAML
fi

# -- Python / venv ------------------------------------------------------------
# The worktree shares the parent repo's venv (worktrees don't have their own).
REPO_ORIGIN="$(cd "$REPO_ROOT" && git rev-parse --path-format=absolute --git-common-dir 2>/dev/null | xargs dirname 2>/dev/null || echo "$REPO_ROOT")"
if [[ -f "$REPO_ORIGIN/venv/bin/python3" ]]; then
  PYTHON="$REPO_ORIGIN/venv/bin/python3"
elif [[ -f "$REPO_ROOT/venv/bin/python3" ]]; then
  PYTHON="$REPO_ROOT/venv/bin/python3"
else
  PYTHON="$(which python3)"
fi
echo "[harness] Python: $PYTHON"

if [[ "$FIXTURE_MODE" == 1 ]]; then
  STARTUP_LOG="$TMPDIR_ROOT/startup.log"
  mkdir -p "$TMPDIR_ROOT/home"
  export AZIZAM_ADMIN_DB="$TMPDIR_ROOT/admin.db"
  export AZIZAM_METRICS_DB="$TMPDIR_ROOT/metrics.db"
  export AZIZAM_RESPONSES_DB="$TMPDIR_ROOT/responses.db"
  export AZIZAM_RECEIPTS_DB="$TMPDIR_ROOT/receipts.db"
  export AZIZAM_MEDIA_JOBS_DB="$TMPDIR_ROOT/media_jobs.db"
  export AZIZAM_CONFIG_PATH="$TMPDIR_ROOT/proxy_config.yml"
  export AZIZAM_ADMIN_SESSION_KEY="p1b-only-session-signing-key-32-bytes-minimum"
  export AZIZAM_IMAGE_CACHE_DIR="$TMPDIR_ROOT/images"
  export AZIZAM_VRAM_ESTIMATES_PATH="$TMPDIR_ROOT/vram_estimates.json"
  export AZIZAM_DRIFT_REPORT_PATH="$TMPDIR_ROOT/drift_status.json"
  export AZIZAM_KEY_ACTIVITY_LOG="$TMPDIR_ROOT/key_activity.jsonl"
  export PYTHONPYCACHEPREFIX="$TMPDIR_ROOT/pycache"
  export TMPDIR="$TMPDIR_ROOT"
  fixture_sandbox_args=(
    # Reuse the hermetic runner's boundary: host filesystem is read-only and
    # this disposable fixture root is the only writable mount.  Keep only the
    # device nodes Python needs; the host network stays shared for the browser
    # journey, while strace proves every traced process-tree endpoint below.
    --die-with-parent --new-session --unshare-user --uid 0 --gid 0
    --disable-userns --assert-userns-disabled --unshare-pid --unshare-ipc
    --ro-bind / / --proc /proc
    --dev-bind /dev/null /dev/null --dev-bind /dev/urandom /dev/urandom --dev-bind /dev/random /dev/random
    --bind "$TMPDIR_ROOT" "$TMPDIR_ROOT" --setenv HOME "$TMPDIR_ROOT/home"
  )
  fixture_sandbox() {
    bwrap "${fixture_sandbox_args[@]}" -- "$@"
  }
  fixture_sandbox env -i PATH="$PATH" HOME="$TMPDIR_ROOT/home" TMPDIR="$TMPDIR" PYTHONPATH="$REPO_ROOT" \
    "$PYTHON" "$REPO_ROOT/tests/compat/python/macro_ux_fixture.py" --assert-os-containment --state-root "$TMPDIR_ROOT" --readonly-root "$REPO_ROOT"
  echo "[harness] Launching authenticated fixture ($FIXTURE_SCENARIO)..."
  setsid strace -f -qq -o "$TMPDIR_ROOT/network-trace.log" -e trace=network bwrap "${fixture_sandbox_args[@]}" -- env -i PATH="$PATH" HOME="$TMPDIR_ROOT/home" TMPDIR="$TMPDIR" LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}" PYTHONPATH="$REPO_ROOT" PYTHONUNBUFFERED=1 PYTHONPYCACHEPREFIX="$PYTHONPYCACHEPREFIX" \
    AZIZAM_ADMIN_DB="$AZIZAM_ADMIN_DB" AZIZAM_METRICS_DB="$AZIZAM_METRICS_DB" AZIZAM_RESPONSES_DB="$AZIZAM_RESPONSES_DB" AZIZAM_RECEIPTS_DB="$AZIZAM_RECEIPTS_DB" AZIZAM_MEDIA_JOBS_DB="$AZIZAM_MEDIA_JOBS_DB" AZIZAM_CONFIG_PATH="$AZIZAM_CONFIG_PATH" AZIZAM_ADMIN_SESSION_KEY="$AZIZAM_ADMIN_SESSION_KEY" AZIZAM_LITELLM_BASE_URL=http://127.0.0.1:9 AZIZAM_LITELLM_API_KEY= TABBY_IDLE_TIMEOUT=0 TTS_ENABLED=false AZIZAM_HOST_TELEMETRY_DISABLED=1 P1B_FIXTURE_SCENARIO="$FIXTURE_SCENARIO" AZIZAM_IMAGE_CACHE_DIR="$AZIZAM_IMAGE_CACHE_DIR" AZIZAM_VRAM_ESTIMATES_PATH="$AZIZAM_VRAM_ESTIMATES_PATH" AZIZAM_DRIFT_REPORT_PATH="$AZIZAM_DRIFT_REPORT_PATH" AZIZAM_KEY_ACTIVITY_LOG="$AZIZAM_KEY_ACTIVITY_LOG" \
    "$PYTHON" "$REPO_ROOT/tests/compat/python/macro_ux_fixture.py" --serve --state-root "$TMPDIR_ROOT" --scenario "$FIXTURE_SCENARIO" >"$STARTUP_LOG" 2>&1 &
  PROXY_PID=$!
  for _ in $(seq 1 200); do [[ -f "$TMPDIR_ROOT/fixture-ready.json" ]] && break; kill -0 "$PROXY_PID" 2>/dev/null || break; sleep 0.1; done
  if [[ ! -f "$TMPDIR_ROOT/fixture-ready.json" ]]; then cat "$STARTUP_LOG" >&2; exit 1; fi
  HARNESS_PORT=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$TMPDIR_ROOT/fixture-ready.json")
  if [[ "$HARNESS_PORT" == 5000 || "$HARNESS_PORT" == 15000 ]]; then echo "ERROR: fixture selected protected port" >&2; exit 1; fi
  for _ in $(seq 1 100); do curl -sf "http://127.0.0.1:$HARNESS_PORT/health" >/dev/null && break; sleep 0.1; done
  curl -sf "http://127.0.0.1:$HARNESS_PORT/health" >/dev/null || { cat "$STARTUP_LOG" >&2; exit 1; }
  sleep 11
  echo "[harness] Fixture ready: http://127.0.0.1:$HARNESS_PORT"
  echo "[harness] Bootstrap: $TMPDIR_ROOT/bootstrap-private.json"
  "$PYTHON" "$REPO_ROOT/tests/compat/python/macro_ux_fixture.py" --verify "$TMPDIR_ROOT/bootstrap-private.json"
  if [[ "$FIXTURE_BROWSER" == 1 ]]; then
    (cd "$REPO_ROOT/web" && P1B_BOOTSTRAP_PATH="$TMPDIR_ROOT/bootstrap-private.json" npm run test:e2e -- tests/playwright/p1b/authenticated-gateway.spec.ts)
  fi
  (cd "$TMPDIR_ROOT" && PYTHONPATH="$REPO_ROOT" AZIZAM_BASE_URL="http://127.0.0.1:$HARNESS_PORT" AZIZAM_ADMIN_KEY="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["compatibility_key"])' "$TMPDIR_ROOT/bootstrap-private.json")" "$PYTHON" -m unittest tests.compat.python.probe_fresh_instance_journey -v) || exit 1
  "$PYTHON" "$REPO_ROOT/tests/compat/python/macro_ux_fixture.py" --assert-network-trace "$TMPDIR_ROOT/network-trace.log" --fixture-port "$HARNESS_PORT"
  exit 0
fi

# -- Launch --------------------------------------------------------------------
export AZIZAM_ADMIN_DB="$TMPDIR_ROOT/admin.db"
export AZIZAM_METRICS_DB="$TMPDIR_ROOT/metrics.db"
export AZIZAM_RESPONSES_DB="$TMPDIR_ROOT/responses.db"
export AZIZAM_RECEIPTS_DB="$TMPDIR_ROOT/receipts.db"
export AZIZAM_MEDIA_JOBS_DB="$TMPDIR_ROOT/media_jobs.db"
export AZIZAM_CONFIG_PATH="$TMPDIR_ROOT/proxy_config.yml"
export AZIZAM_PORT="$HARNESS_PORT"
export AZIZAM_ADMIN_SESSION_KEY="harness-test-session-key-not-for-production"
# Fresh-instance probes must not borrow the operator's live LiteLLM sidecar.
# Keep LiteLLM deliberately unreachable unless a test opts in explicitly.
export AZIZAM_LITELLM_BASE_URL="${AZIZAM_LITELLM_BASE_URL:-http://127.0.0.1:15999}"
export AZIZAM_LITELLM_API_KEY="${AZIZAM_LITELLM_API_KEY:-}"
# Silence noisy tabbyapi health checks that would fail on a fresh host
export TABBY_IDLE_TIMEOUT=0
export TTS_ENABLED=false
# This is someone's real desktop/dev box, not the fresh-operator's machine --
# never leak its disk/thermal state into the isolated instance's health.
export AZIZAM_HOST_TELEMETRY_DISABLED=1

echo "[harness] Launching gateway (Ctrl-C or --smoke auto-stops)..."
STARTUP_LOG="$TMPDIR_ROOT/startup.log"
"$PYTHON" "$REPO_ROOT/proxy.py" > "$STARTUP_LOG" 2>&1 &
PROXY_PID=$!
echo "[harness] PID: $PROXY_PID"

# Wait for the server to be ready
READY=0
for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:$HARNESS_PORT/health" >/dev/null 2>&1; then
    READY=1
    echo "[harness] Gateway ready after ${i}s"
    break
  fi
  sleep 1
done

if [[ $READY -eq 0 ]]; then
  echo "[harness] ERROR: Gateway did not become ready within 30s" >&2
  echo "[harness] Startup log:" >&2
  cat "$STARTUP_LOG" >&2
  kill "$PROXY_PID" 2>/dev/null || true
  exit 1
fi

# Fail if any store initialised under the repo data/ directory
REPO_DATA="$REPO_ROOT/data"
if grep -qF "$REPO_DATA" "$STARTUP_LOG" 2>/dev/null; then
  echo "[harness] ERROR: startup log references repo data/ directory:" >&2
  grep -F "$REPO_DATA" "$STARTUP_LOG" >&2
  kill "$PROXY_PID" 2>/dev/null || true
  exit 1
fi
echo "[harness] Store isolation check passed (no reference to $REPO_DATA in startup log)"

if [[ $SMOKE_MODE -eq 1 ]]; then
  echo ""
  echo "-- Smoke checks ------------------------------------------------------"

  PASS=0; FAIL=0

  check() {
    local desc="$1"; local url="$2"; local expected_status="${3:-200}"
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$HARNESS_PORT$url")
    if [[ "$status" == "$expected_status" ]]; then
      echo "  PASS  $desc  [$status]"
      PASS=$((PASS+1))
    else
      echo "  FAIL  $desc  [got $status, expected $expected_status]"
      FAIL=$((FAIL+1))
    fi
  }

  check "GET /health"                          /health
  check "GET /v1/models (no auth)"             /v1/models
  check "GET /gateway/v1/capabilities"         /gateway/v1/capabilities
  check "GET /setup (first-run bootstrap UI)"  /setup

  echo ""
  echo "-- Store DB checks ---------------------------------------------------"
  for db_name in admin.db metrics.db responses.db receipts.db media_jobs.db; do
    if [[ -f "$TMPDIR_ROOT/$db_name" ]]; then
      echo "  PASS  $db_name created at $TMPDIR_ROOT/$db_name"
      PASS=$((PASS+1))
    else
      echo "  WARN  $db_name not yet created at $TMPDIR_ROOT/$db_name (may be lazy-init)"
    fi
  done

  echo ""
  echo "-- Live DB isolation check -------------------------------------------"
  if [[ ! -f "$LIVE_DB" ]] || ! sqlite3 "$LIVE_DB" "select count(*) from sqlite_master;" >/dev/null 2>&1; then
    echo "  SKIP  Live DB not present (good for CI)"
  else
    # Verify our harness DB is a different inode than the live DB
    LIVE_INODE=$(stat -c %i "$LIVE_DB" 2>/dev/null || echo "missing")
    HARNESS_INODE=$(stat -c %i "$TMPDIR_ROOT/admin.db" 2>/dev/null || echo "missing")
    if [[ "$LIVE_INODE" != "$HARNESS_INODE" ]]; then
      echo "  PASS  Harness admin.db is distinct from live admin.db"
      PASS=$((PASS+1))
    else
      echo "  FAIL  Harness admin.db appears to be the same file as the live DB!"
      FAIL=$((FAIL+1))
    fi
  fi

  echo ""
  echo "Results: $PASS passed, $FAIL failed"
  kill "$PROXY_PID" 2>/dev/null || true

  if [[ $FAIL -gt 0 ]]; then
    exit 1
  fi
else
  echo "[harness] Running. Ctrl-C to stop."
  tail -f "$STARTUP_LOG" &
  TAIL_PID=$!
  wait "$PROXY_PID"
  kill "$TAIL_PID" 2>/dev/null || true
fi

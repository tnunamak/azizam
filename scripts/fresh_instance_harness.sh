#!/usr/bin/env bash
# fresh_instance_harness.sh — spin up an isolated AI Gateway instance for fresh-instance testing.
#
# WHAT THIS DOES
#   1. Creates a temp directory under /tmp for the admin DB and session key.
#   2. Writes a minimal proxy_config.yml into a temp dir and runs proxy.py from there,
#      so CONFIG_PATH (hardcoded to __file__/../proxy_config.yml) picks up the fresh config.
#   3. Binds to HARNESS_PORT (default 15000) — never the live port 5000.
#   4. Fails loudly if it detects the live admin DB path in the environment.
#
# SOURCES OF TRUTH (documented explicitly)
#   DB:   AI_GATEWAY_ADMIN_DB  — temp path created here; never touches live data/admin.db
#   File: proxy_config.yml     — written fresh from MINIMAL_CONFIG below (no env override exists)
#   Env:  OPENAI_PROXY_PORT    — set to HARNESS_PORT
#   Env:  AI_GATEWAY_ADMIN_SESSION_KEY — set to a fixed test-only secret
#
# KNOWN GAPS
#   - receipt_store (data/receipts.db) and media_jobs (data/media_jobs.db) and
#     metrics_store have no env override for their DB paths; they write to the
#     worktree's data/ directory (which is clean/empty) rather than /tmp.
#   - proxy_config.yml has no env override — this script writes a fresh minimal
#     copy to the temp dir and runs proxy.py from there.
#   - TABBY_CONFIG_FILE (/home/tnunamak/applications/tabbyapi/config.yml) is
#     hardcoded; it is optional and will simply be skipped if absent.
#
# USAGE
#   ./scripts/fresh_instance_harness.sh [--smoke] [--port PORT]
#
#   --smoke   Run smoke checks (health + admin endpoints) then shut down.
#   --port N  Use port N instead of 15000.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HARNESS_PORT="${HARNESS_PORT:-15000}"
SMOKE_MODE=0

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --smoke) SMOKE_MODE=1; shift ;;
    --port)  HARNESS_PORT="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ── Safety guard ──────────────────────────────────────────────────────────────
LIVE_DB="$REPO_ROOT/data/admin.db"
if [[ "${AI_GATEWAY_ADMIN_DB:-}" == "$LIVE_DB" ]]; then
  echo "ERROR: AI_GATEWAY_ADMIN_DB is set to the live DB path. Aborting." >&2
  exit 1
fi
if [[ "${OPENAI_PROXY_ADMIN_DB:-}" == "$LIVE_DB" ]]; then
  echo "ERROR: OPENAI_PROXY_ADMIN_DB is set to the live DB path. Aborting." >&2
  exit 1
fi
if [[ "${OPENAI_PROXY_PORT:-}" == "5000" || "${HARNESS_PORT}" == "5000" ]]; then
  echo "ERROR: Refusing to bind on live port 5000. Use --port or set HARNESS_PORT." >&2
  exit 1
fi

# ── Temp workspace ─────────────────────────────────────────────────────────────
TMPDIR_ROOT=$(mktemp -d /tmp/agw-fresh-XXXXXX)
trap 'echo "[harness] Cleaning up $TMPDIR_ROOT"; rm -rf "$TMPDIR_ROOT"' EXIT

echo "[harness] Temp workspace: $TMPDIR_ROOT"
echo "[harness] Port:           $HARNESS_PORT"
echo "[harness] Admin DB:       $TMPDIR_ROOT/admin.db"

# ── Minimal proxy_config.yml ──────────────────────────────────────────────────
# Generic backends pointing at localhost ports that won't be up — the gateway
# starts fine and reports them as unreachable. This exercises the bootstrap
# path without any Tim-specific service details.
cat > "$TMPDIR_ROOT/proxy_config.yml" <<'YAML'
proxy:
  host: 127.0.0.1
  port: 15000  # overridden by OPENAI_PROXY_PORT env below

# No auth required for fresh-instance smoke: lets health + admin endpoints work
# without needing to provision an App credential first.
auth:
  enabled: false

admin:
  session_signing_key: harness-test-session-key-not-for-production

backends:
  # tabby entry must be present with managed: false to prevent the gateway
  # from trying to spawn a TabbyAPI subprocess (TABBY_MANAGED defaults to true
  # when the key is absent). The host/port are placeholders; the backend will
  # be unreachable but the gateway starts and serves all non-LLM routes.
  tabby:
    name: TabbyAPI (harness placeholder — not started)
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

# Symlink proxy.py and gateway/ into the temp dir so CONFIG_PATH resolves
# to $TMPDIR_ROOT/proxy_config.yml (CONFIG_PATH = Path(__file__).parent / "proxy_config.yml").
ln -s "$REPO_ROOT/proxy.py"   "$TMPDIR_ROOT/proxy.py"
ln -s "$REPO_ROOT/gateway"    "$TMPDIR_ROOT/gateway"

# ── Python / venv ──────────────────────────────────────────────────────────────
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

# ── Launch ────────────────────────────────────────────────────────────────────
export AI_GATEWAY_ADMIN_DB="$TMPDIR_ROOT/admin.db"
export OPENAI_PROXY_PORT="$HARNESS_PORT"
export AI_GATEWAY_ADMIN_SESSION_KEY="harness-test-session-key-not-for-production"
# Silence noisy tabbyapi health checks that would fail on a fresh host
export TABBY_IDLE_TIMEOUT=0
export TTS_ENABLED=false

echo "[harness] Launching gateway (Ctrl-C or --smoke auto-stops)..."
cd "$TMPDIR_ROOT"
"$PYTHON" proxy.py &
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
  kill "$PROXY_PID" 2>/dev/null || true
  exit 1
fi

if [[ $SMOKE_MODE -eq 1 ]]; then
  echo ""
  echo "── Smoke checks ──────────────────────────────────────────────────────"

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
  echo "── Admin DB check ────────────────────────────────────────────────────"
  if [[ -f "$TMPDIR_ROOT/admin.db" ]]; then
    echo "  PASS  admin.db created at $TMPDIR_ROOT/admin.db"
    PASS=$((PASS+1))
  else
    echo "  FAIL  admin.db not found at $TMPDIR_ROOT/admin.db"
    FAIL=$((FAIL+1))
  fi

  echo ""
  echo "── Live DB isolation check ───────────────────────────────────────────"
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
  wait "$PROXY_PID"
fi

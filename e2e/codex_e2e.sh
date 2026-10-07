#!/usr/bin/env bash
# Real Codex -> Responses -> branch gateway -> Chat backend tool-loop smoke test.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Resolve the real codex binary BEFORE HOME is swapped: a mise shim (or a shell wrapper)
# cannot run under the scratch HOME used below.
codex_bin=${CODEX_BIN:-$(command -v codex || true)}
if [[ "$codex_bin" == */shims/* ]] && command -v mise >/dev/null; then codex_bin=$(mise which codex); fi
if [[ -z "$codex_bin" ]]; then
  echo "SKIP: codex is not installed" >&2
  exit 0
fi
state_base=${AZIZAM_E2E_STATE_ROOT:-"$HOME/.tmp/azizam-codex-e2e"}
mkdir -p "$state_base"
state=$(mktemp -d "$state_base/run.XXXXXXXX")
cleanup() {
  local code=$?
  for pid in "${gateway_pid:-}" "${backend_pid:-}"; do
    [[ -n "$pid" ]] && kill -- "-$pid" 2>/dev/null || true
  done
  for pid in "${gateway_pid:-}" "${backend_pid:-}"; do
    [[ -n "$pid" ]] && wait "$pid" 2>/dev/null || true
  done
  echo "Codex e2e evidence retained at $state" >&2
  exit "$code"
}
trap cleanup EXIT INT TERM

uv_bin=$(command -v uv || true)
[[ -n "$uv_bin" ]] || { echo "uv is required for the isolated e2e interpreter" >&2; exit 127; }
python_seed=${AZIZAM_E2E_PYTHON_SEED:-"$HOME/.tmp/azizam-hermetic-python/cpython-3.12.13-linux-x86_64-gnu/bin/python3.12"}
[[ -x "$python_seed" ]] || { echo "set AZIZAM_E2E_PYTHON_SEED to a CPython 3.12 interpreter" >&2; exit 2; }
python_bin="$state/venv/bin/python"
env -i "HOME=$state/uv-home" "PATH=$(dirname "$uv_bin"):/usr/bin:/bin" "UV_CACHE_DIR=$HOME/.tmp/hermetic-uv-cache" UV_NO_CONFIG=1 \
  "$uv_bin" venv --no-project --no-managed-python --no-python-downloads --python "$python_seed" "$state/venv" >/dev/null
env -i "HOME=$state/uv-home" "PATH=$(dirname "$uv_bin"):/usr/bin:/bin" "UV_CACHE_DIR=$HOME/.tmp/hermetic-uv-cache" UV_NO_CONFIG=1 \
  "$uv_bin" pip sync --offline --no-build --strict --require-hashes --link-mode copy --python "$python_bin" "$repo/tests/requirements-hermetic.lock" >/dev/null

read -r gateway_port backend_port < <("$python_bin" - <<'PY'
import socket
for port in range(18300, 18399):
    probes = [socket.socket(), socket.socket()]
    try:
        probes[0].bind(("127.0.0.1", port)); probes[1].bind(("127.0.0.1", port + 1))
        print(port, port + 1); break
    except OSError:
        pass
    finally:
        for probe in probes: probe.close()
else: raise SystemExit("no free e2e port pair in 18300-18399")
PY
)

home="$state/home"; codex_home="$state/codex-home"; scratch="$state/scratch"
mkdir -p "$home" "$codex_home" "$scratch"
git -C "$scratch" init -q
git -C "$scratch" config user.email azizam-e2e@example.test
git -C "$scratch" config user.name azizam-e2e

cat > "$state/config.yml" <<EOF
proxy:
  host: 127.0.0.1
  port: $gateway_port
auth:
  enabled: true
  lan_bypass_enabled: false
backends:
  tabby: {enabled: false, host: 127.0.0.1, port: $backend_port}
  kobold: {enabled: false, host: 127.0.0.1, port: $backend_port}
  comfyui: {enabled: false, host: 127.0.0.1, port: $backend_port}
  embeddings: {enabled: false, host: 127.0.0.1, port: $backend_port}
  tts: {enabled: false, host: 127.0.0.1, port: $backend_port}
  stt: {enabled: false, host: 127.0.0.1, port: $backend_port}
  litellm: {enabled: true, base_url: "http://127.0.0.1:$backend_port", host: 127.0.0.1, port: $backend_port}
model_aliases:
  chat:
    stable:
      fast: azizam-e2e
images:
  cache_dir: $state/images
  vram_estimates_file: $state/vram.json
EOF
cat > "$state/model-catalog.json" <<'EOF'
{"models":[{"slug":"azizam-e2e","display_name":"Azizam e2e","description":"Isolated Azizam Responses test model.","base_instructions":"You are the Azizam e2e coding agent.","default_reasoning_level":null,"supported_reasoning_levels":[],"shell_type":"unified_exec","visibility":"list","supported_in_api":true,"priority":1,"additional_speed_tiers":[],"service_tiers":[],"default_service_tier":null,"available_access_programs":null,"availability_nux":null,"upgrade":null,"model_messages":null,"include_skills_usage_instructions":false,"include_plugin_usage_instructions":false,"include_apps_usage_instructions":false,"supports_reasoning_summary_parameter":true,"default_reasoning_summary":"auto","support_verbosity":false,"default_verbosity":null,"apply_patch_tool_type":"freeform","web_search_tool_type":"text","truncation_policy":{"mode":"bytes","limit":10000},"supports_image_detail_original":false,"context_window":null,"max_context_window":null,"auto_compact_token_limit":null,"comp_hash":null,"effective_context_window_percent":95,"experimental_supported_tools":[],"supports_search_tool":false,"supports_experimental_context":false,"use_responses_lite":false,"supports_reasoning_effort_updates":false,"guardian":null,"node_repl_auto_review_required":false,"node_repl_disabled":false,"auto_review_model_override":null,"model_specialty":null,"tool_mode":null,"multi_agent_version":null,"multi_agent_reasoning_effort":null}]}
EOF
cat > "$codex_home/config.toml" <<EOF
model = "azizam-e2e"
model_provider = "azizam"
web_search = "disabled"
model_catalog_json = "$state/model-catalog.json"
[features]
plugins = false
[model_providers.azizam]
name = "Azizam"
base_url = "http://127.0.0.1:$gateway_port/v1"
wire_api = "responses"
env_key = "AZIZAM_E2E_KEY"
supports_standalone_web_search = false
[mcp_servers.azizam_e2e]
command = "$python_bin"
args = ["$repo/e2e/codex_e2e_mcp.py"]
EOF

base_env=(env -i "PATH=$PATH" "HOME=$home" "PYTHONPATH=$repo" "PYTHONDONTWRITEBYTECODE=1" "AZIZAM_CONFIG_PATH=$state/config.yml" "AZIZAM_LITELLM_BASE_URL=http://127.0.0.1:$backend_port" "AZIZAM_GRACEFUL_SHUTDOWN_S=1" "TTS_ENABLED=false" "STT_ENABLED=false" "AZIZAM_ADMIN_DB=$state/admin.db" "AZIZAM_METRICS_DB=$state/metrics.db" "AZIZAM_RESPONSES_DB=$state/responses.db" "AZIZAM_RECEIPTS_DB=$state/receipts.db" "AZIZAM_MEDIA_JOBS_DB=$state/media-jobs.db" "AZIZAM_E2E_DUMMY_KEY=azizam-e2e-dummy-key" "CODEX_E2E_GATEWAY_LOG=$state/gateway-requests.jsonl" "CODEX_E2E_BACKEND_LOG=$state/backend-requests.jsonl" "UI_E2E_ALLOWED_PORTS=$gateway_port,$backend_port")
setsid "${base_env[@]}" "$python_bin" -m uvicorn e2e.codex_e2e_backend:app --host 127.0.0.1 --port "$backend_port" >"$state/backend.log" 2>&1 & backend_pid=$!
setsid "${base_env[@]}" "$python_bin" -m e2e.codex_e2e_gateway "$gateway_port" "$backend_port" >"$state/gateway.log" 2>&1 & gateway_pid=$!

for _ in {1..100}; do
  if curl --fail --silent "http://127.0.0.1:$gateway_port/health" >/dev/null; then break; fi
  sleep .1
done
curl --fail --silent "http://127.0.0.1:$gateway_port/health" >/dev/null || { tail -80 "$state/gateway.log" >&2; exit 1; }

command=("$codex_bin" exec --json --sandbox danger-full-access --skip-git-repo-check --ephemeral --color never -C "$scratch" "Use apply_patch, and only apply_patch, to create hello.txt containing exactly: hello from Azizam e2e. Then state that it was created.")
printf '%q ' "${command[@]}" > "$state/codex-command.txt"; printf '\n' >> "$state/codex-command.txt"
(cd "$scratch" && "${base_env[@]}" "CODEX_HOME=$codex_home" "PATH=$(dirname "$codex_bin"):/usr/bin:/bin" "AZIZAM_E2E_KEY=azizam-e2e-dummy-key" "RUST_LOG=warn" timeout 120s "${command[@]}" </dev/null >"$state/codex-transcript.jsonl" 2>"$state/codex-stderr.log")

"$python_bin" - "$state" <<'PY'
import hashlib, json, sys
from pathlib import Path
state = Path(sys.argv[1])
requests = [json.loads(line) for line in (state / "gateway-requests.jsonl").read_text().splitlines()]
if not requests or not any(tool.get("type") == "namespace" and tool.get("name") == "mcp__azizam_e2e" for request in requests for tool in request.get("tools", []) if isinstance(tool, dict)):
    raise SystemExit("no observed Azizam Responses namespace declaration")
if not any(tool.get("type") == "custom" and tool.get("name") == "apply_patch" for tool in requests[0].get("tools", []) if isinstance(tool, dict)):
    raise SystemExit("Codex did not declare the Azizam custom apply_patch tool")
expected_auth = hashlib.sha256(b"Bearer azizam-e2e-dummy-key").hexdigest()
if not all(request.get("_azizam_e2e", {}).get("authorization_hash") == expected_auth and request.get("_azizam_e2e", {}).get("status") == 200 for request in requests):
    raise SystemExit("Azizam did not accept the fixture dummy key")
backend = [json.loads(line) for line in (state / "backend-requests.jsonl").read_text().splitlines()]
if len(backend) < 3 or not any(message.get("role") == "tool" for request in backend for message in request.get("messages", []) if isinstance(message, dict)):
    raise SystemExit("Azizam custom-tool continuation did not reach the fake Chat backend")
def typed_items(value):
    if isinstance(value, dict):
        if "type" in value:
            yield value
        for child in value.values():
            yield from typed_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from typed_items(child)
continuation = [item for request in requests[1:] for item in typed_items(request.get("input"))]
if not any(item.get("type") == "custom_tool_call" and item.get("name") == "apply_patch" for item in continuation):
    raise SystemExit("Azizam did not receive the custom apply_patch call continuation")
if not any(item.get("type") == "custom_tool_call_output" and "Success" in str(item.get("output")) for item in continuation):
    raise SystemExit("Azizam did not receive the custom apply_patch output continuation")
if not any(item.get("type") == "function_call" and item.get("namespace") == "mcp__azizam_e2e" and item.get("name") == "namespace_probe" for item in continuation):
    raise SystemExit("Azizam did not receive the namespace function call continuation")
if not any(item.get("type") == "function_call_output" and "azizam namespace probe" in str(item.get("output")) for item in continuation):
    raise SystemExit("Azizam did not receive the namespace function output continuation")
if (state / "scratch" / "hello.txt").read_text() != "hello from Azizam e2e\n":
    raise SystemExit("apply_patch did not create the expected hello.txt")
transcript = (state / "codex-transcript.jsonl").read_text()
if "Created hello.txt" not in transcript:
    raise SystemExit("Codex final message was not observed")
events = [json.loads(line) for line in transcript.splitlines()]
if not any(event.get("type") == "turn.completed" for event in events) or any(event.get("type") == "turn.failed" for event in events):
    raise SystemExit("Codex did not complete the Azizam e2e turn")
if "unsupported call" in transcript.lower():
    raise SystemExit("Codex reported an unsupported tool call")
print("PASS: namespace declaration, custom apply_patch round trip, and hello.txt verified")
PY

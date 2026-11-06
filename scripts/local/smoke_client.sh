#!/usr/bin/env bash

# Smoke-test the local proxy as an OpenAI-compatible client would see it.
#
# Usage:
#   AZIZAM_API_KEY=opk_... ./scripts/local/smoke_client.sh
#   AZIZAM_API_KEY=opk_... AZIZAM_BASE=https://ai.example.com ./scripts/local/smoke_client.sh
#
# Never paste a real opk_... value as a literal argument; export it from your
# secret store. This script prints only response prefixes, never the key.

set -euo pipefail

BASE="${AZIZAM_BASE:-http://127.0.0.1:5000}"
MODEL="${AZIZAM_SMOKE_MODEL:-auto}"

if [[ -z "${AZIZAM_API_KEY:-}" ]]; then
  echo "AZIZAM_API_KEY is not set; create an App key from /setup, /, or /admin." >&2
  exit 2
fi

auth=(-H "Authorization: Bearer ${AZIZAM_API_KEY}")

step() { printf '\n=== %s ===\n' "$*"; }
preview() { head -c 400; printf '\n'; }

step "GET ${BASE}/health (public)"
curl -fsS "${BASE}/health" | preview

step "GET ${BASE}/v1/models"
curl -fsS "${auth[@]}" "${BASE}/v1/models" | preview

step "POST ${BASE}/v1/chat/completions (model=${MODEL})"
curl -fsS "${auth[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"stream\":false}" \
  "${BASE}/v1/chat/completions" | preview

step "GET ${BASE}/gateway/v1/capabilities"
curl -fsS -o /tmp/.opx_caps.$$ -w '%{http_code}\n' "${auth[@]}" "${BASE}/gateway/v1/capabilities" || true
head -c 400 /tmp/.opx_caps.$$ 2>/dev/null || true
printf '\n'
rm -f /tmp/.opx_caps.$$

step "GET ${BASE}/gateway/v1/receipts?limit=5"
curl -fsS -o /tmp/.opx_rec.$$ -w '%{http_code}\n' "${auth[@]}" "${BASE}/gateway/v1/receipts?limit=5" || true
head -c 400 /tmp/.opx_rec.$$ 2>/dev/null || true
printf '\n'
rm -f /tmp/.opx_rec.$$

echo
echo "Smoke done. Non-2xx on /gateway/v1/* means auth, profile policy, or gateway storage needs investigation."

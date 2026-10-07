#!/bin/bash

# E2E test for azizam endpoints
# Usage: AZIZAM_API_KEY=opk_... ./test_e2e.sh [proxy_url]

PROXY="${1:-http://127.0.0.1:5000}"
API_KEY="${AZIZAM_API_KEY:-${2:-}}"
PASS=0
FAIL=0

if [ -z "$API_KEY" ]; then
    echo "Set AZIZAM_API_KEY or pass the API key as the second argument."
    exit 2
fi

AUTH_HEADER="-H 'Authorization: Bearer $API_KEY'"

green() { echo -e "\033[32m$1\033[0m"; }
red() { echo -e "\033[31m$1\033[0m"; }

test_endpoint() {
    local name="$1"
    local cmd="$2"
    local expect="$3"
    
    printf "%-25s" "$name"
    
    start=$(date +%s.%N)
    result=$(eval "$cmd" 2>&1)
    end=$(date +%s.%N)
    elapsed=$(echo "$end - $start" | bc)
    
    if echo "$result" | grep -q "$expect"; then
        green "PASS (${elapsed}s)"
        ((PASS++))
        return 0
    else
        red "FAIL"
        echo "  Expected: $expect"
        echo "  Got: ${result:0:200}..."
        ((FAIL++))
        return 1
    fi
}

echo "=== LLM Proxy E2E Tests ==="
echo "Proxy: $PROXY"
echo ""

# Health
test_endpoint "Health" \
    "curl -s $PROXY/health" \
    "healthy"

# TTS - generate test audio
echo ""
echo "--- Audio Services ---"
test_endpoint "TTS (generate)" \
    "curl -s -X POST $PROXY/v1/audio/speech $AUTH_HEADER -H 'Content-Type: application/json' -d '{\"input\": \"Hello, this is a test.\", \"voice\": \"alloy\"}' -o /tmp/tts_test.mp3 -w '%{http_code}'" \
    "200"

# STT - transcribe the generated audio
test_endpoint "STT (transcribe)" \
    "curl -s -X POST $PROXY/v1/audio/transcriptions $AUTH_HEADER -F 'file=@/tmp/tts_test.mp3' -F 'model=whisper-1'" \
    "text"

# Image generation
echo ""
echo "--- Image Generation ---"
test_endpoint "Image (4B 512x512)" \
    "curl -s -X POST $PROXY/v1/images/generations $AUTH_HEADER -H 'Content-Type: application/json' -d '{\"model\": \"flux2-klein-4b\", \"prompt\": \"a cat\", \"size\": \"512x512\"}' | python3 -c \"import sys,json; d=json.load(sys.stdin); print(f'created={d.get(\\\"created\\\")}, images={len(d.get(\\\"data\\\",[]))}')\"" \
    "images=1"

# LLM
echo ""
echo "--- LLM ---"
test_endpoint "Chat completion" \
    "curl -s -X POST $PROXY/v1/chat/completions $AUTH_HEADER -H 'Content-Type: application/json' -d '{\"model\": \"gpt-4o\", \"messages\": [{\"role\": \"user\", \"content\": \"say hi\"}], \"max_tokens\": 5}'" \
    "choices"

echo ""
echo "========================================"
echo "Results: $(green "$PASS passed"), $(red "$FAIL failed")"
echo "========================================"

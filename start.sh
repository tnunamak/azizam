#!/bin/bash
# OpenAI Proxy service starter
# Unified OpenAI-compatible API for TabbyAPI/KoboldCpp/TTS/STT/ComfyUI backends
# Served externally at openai.vivid.fish (via Traefik on 192.168.1.4)

cd "$(dirname "${BASH_SOURCE[0]}")"

# Use proxy's own isolated venv
if [ ! -d "venv" ]; then
    echo "Creating venv..."
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
fi

source venv/bin/activate

# Config via environment (override proxy_config.yml defaults)
export OPENAI_PROXY_PORT="${OPENAI_PROXY_PORT:-5000}"
export TABBY_IDLE_TIMEOUT="${TABBY_IDLE_TIMEOUT:-1800}"
export TTS_ENABLED="${TTS_ENABLED:-true}"
export TTS_HOST="${TTS_HOST:-http://127.0.0.1:8001}"

exec python3 proxy.py "$@"

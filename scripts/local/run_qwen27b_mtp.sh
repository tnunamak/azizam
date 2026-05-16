#!/usr/bin/env bash
# Run Qwen3.6-27B with TurboQuant KV cache and MTP speculative decoding.
#
# This intentionally uses the model file under /media/windows/AI Models, not the
# Hugging Face cache, so wiping ~/.cache/huggingface does not break the service.

set -euo pipefail

LLAMA_MTP_APP="${LLAMA_MTP_APP:-/home/tnunamak/applications/llama-tq-mtp}"
LLAMA_MTP_BIN="${LLAMA_MTP_BIN:-$LLAMA_MTP_APP/build/bin/llama-server}"
LLAMA_MTP_MODEL="${LLAMA_MTP_MODEL:-/media/windows/AI Models/LLMs/GGUF/MTP/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf}"
LLAMA_MTP_PORT="${LLAMA_MTP_PORT:-5051}"
LLAMA_MTP_HOST="${LLAMA_MTP_HOST:-127.0.0.1}"
LLAMA_MTP_CTX="${LLAMA_MTP_CTX:-196608}"
LLAMA_MTP_PARALLEL="${LLAMA_MTP_PARALLEL:-1}"
LLAMA_MTP_GPU_LAYERS="${LLAMA_MTP_GPU_LAYERS:-999}"
LLAMA_MTP_CTK="${LLAMA_MTP_CTK:-turbo4}"
LLAMA_MTP_CTV="${LLAMA_MTP_CTV:-turbo4}"
LLAMA_MTP_DRAFT_N_MAX="${LLAMA_MTP_DRAFT_N_MAX:-4}"
LLAMA_MTP_VISIBLE_DEVICES="${LLAMA_MTP_VISIBLE_DEVICES:-1}"
LLAMA_MTP_AUTO_ASYMMETRIC="${LLAMA_MTP_AUTO_ASYMMETRIC:-0}"

if [ ! -x "$LLAMA_MTP_BIN" ]; then
    echo "missing llama-server binary: $LLAMA_MTP_BIN" >&2
    exit 1
fi

if [ ! -f "$LLAMA_MTP_MODEL" ]; then
    echo "missing MTP GGUF model: $LLAMA_MTP_MODEL" >&2
    exit 1
fi

export CUDA_VISIBLE_DEVICES="$LLAMA_MTP_VISIBLE_DEVICES"
export TURBO_AUTO_ASYMMETRIC="$LLAMA_MTP_AUTO_ASYMMETRIC"

cd "$LLAMA_MTP_APP"

exec "$LLAMA_MTP_BIN" \
    --model "$LLAMA_MTP_MODEL" \
    --port "$LLAMA_MTP_PORT" \
    --host "$LLAMA_MTP_HOST" \
    --n-gpu-layers "$LLAMA_MTP_GPU_LAYERS" \
    --ctx-size "$LLAMA_MTP_CTX" \
    --parallel "$LLAMA_MTP_PARALLEL" \
    --cache-type-k "$LLAMA_MTP_CTK" \
    --cache-type-v "$LLAMA_MTP_CTV" \
    --jinja \
    --spec-type mtp \
    --spec-draft-n-max "$LLAMA_MTP_DRAFT_N_MAX" \
    --no-warmup \
    "$@"

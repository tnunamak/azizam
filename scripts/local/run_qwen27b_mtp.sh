#!/usr/bin/env bash
# Run Qwen3.6-27B with TurboQuant KV cache and MTP speculative decoding.
#
# This intentionally uses the model file under /media/windows/AI Models, not the
# Hugging Face cache, so wiping ~/.cache/huggingface does not break the service.
#
# STATUS (2026-06-17): llama-mtp.service is DISABLED and does not work on the
# latest llama-tq-mtp. The fork (QuinsZouls/llama-cpp-turboquant) synced to a
# newer upstream that reorganized the speculative-decoding subsystem, and the
# MTP orchestration layer in common/ (the common_speculative_state_mtp class +
# init wiring + the "mtp" --spec-type string) fell out of the merge — left as
# `// TODO: add MTP here` on origin/llama-next HEAD (71dcf85). The engine-level
# support (src/llama-mtp.h, llama-context hooks) survived, so this looks like a
# WIP regression mid-rebase, not abandonment (the MTP GGUF was re-quantized
# 2026-06-06, after the merge). llama-tq-mtp is therefore pinned at the last
# working ref 1e5a46d ("llama-next-b9265-1e5a46d"). Do NOT bump it until a
# future llama-next commit re-wires "mtp" into the new framework — verify with:
#   build/bin/llama-server --spec-type mtp --model /dev/null   # must NOT say "unknown speculative type"
#
# CAVEAT if you ever enable this: LLAMA_MTP_PORT below defaults to 5051, which
# is the LIVE llama-bee chat backend's port. Override LLAMA_MTP_PORT (and pick a
# non-conflicting GPU via LLAMA_MTP_VISIBLE_DEVICES) before starting, or it will
# collide with the running backend.

set -euo pipefail

LLAMA_MTP_APP="${LLAMA_MTP_APP:-$HOME/applications/llama-tq-mtp}"
LLAMA_MTP_BIN="${LLAMA_MTP_BIN:-$LLAMA_MTP_APP/build/bin/llama-server}"
LLAMA_MTP_MODEL="${LLAMA_MTP_MODEL:-/media/windows/AI Models/LLMs/GGUF/Qwen3.6-27B-UD-Q4_K_XL-mtp.gguf}"
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

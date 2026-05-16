# Qwen3.6 27B TurboQuant+MTP Local Backend

This documents the context-gain Qwen3.6-27B backend: TurboQuant KV cache plus
the model's native MTP speculative decoding head.

## Files

- TurboQuant+MTP llama.cpp checkout:
  `/home/tnunamak/applications/llama-tq-mtp`
- Exact fork/tag tested: `QuinsZouls/llama-cpp-turboquant`,
  `llama-next-b9265-1e5a46d`
- MTP model:
  `/media/windows/AI Models/LLMs/GGUF/MTP/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf`
- Runner: `scripts/local/run_qwen27b_mtp.sh`
- User service: `llama-mtp.service`
- Benchmark harness: `scripts/local/bench_llm_endpoint.py`

The model lives under `/media/windows/AI Models`, not the Hugging Face cache.
Wiping `~/.cache/huggingface` must not affect this service.

## Recommendation

Keep `llama-turbo.service` as the default for now. Use `llama-mtp.service` as an
opt-in text backend when long text context and throughput matter more than
vision.

Why this is not the default yet:

- The current default service is vision-capable via `--mmproj`.
- MTP and mmproj were not validated together locally.
- The context-gain profile requires symmetric `turbo4/turbo4` KV. The fork
  normally auto-upgrades K back to `q8_0` on GQA models to reduce quality risk;
  the runner disables that with `TURBO_AUTO_ASYMMETRIC=0`.
- The safer `q8_0/turbo4` profile supports 131k context with MTP and a small
  throughput win, but it does not gain context over the current default.

## Runner Defaults

`scripts/local/run_qwen27b_mtp.sh` defaults to:

- app: `/home/tnunamak/applications/llama-tq-mtp`
- port: `5051`
- GPU: `CUDA_VISIBLE_DEVICES=1`
- context: `196608`
- slots: `--parallel 1`
- KV cache: `--cache-type-k turbo4 --cache-type-v turbo4`
- TurboQuant GQA safety override: `TURBO_AUTO_ASYMMETRIC=0`
- MTP: `--spec-type mtp --spec-draft-n-max 4`
- warmup disabled

The service conflicts with `llama-turbo.service`, `tabbyapi.service`, and
`koboldcpp.service`, so only one GGUF backend owns port 5051.

## Tested Results

All results used GPU1, full GPU offload, the same benchmark prompt,
`max_tokens=256`, temperature 0, and Qwen thinking disabled through chat
template kwargs.

| Backend | Context | KV | MTP | Generation tok/s | Draft acceptance | Notes |
| --- | ---: | --- | --- | ---: | ---: | --- |
| old `llama-turbo.service` | 131k | `q8_0/turbo4` | no | 38.81 | - | current default, vision-capable |
| TurboQuant+MTP safe KV | 131k | `q8_0/turbo4` | yes | 40.55 | 32.7% | small speed gain, no context gain |
| TurboQuant+MTP context-gain | 196k | `turbo4/turbo4` | yes | 46.98 | 42.1% | proven context-gain profile |
| upstream llama.cpp, MTP tuned | 32k | `q8_0/q8_0` | yes | 43.17 | ~39.7% | fast, but not enough context |

The proven context-gain profile gives 196,608 tokens, a 50% context increase
over the 131,072-token default, and about a 21% generation-throughput increase
on this short benchmark.

## Validation Commands

Isolated test without touching the production port:

```bash
TURBO_AUTO_ASYMMETRIC=0 CUDA_VISIBLE_DEVICES=1 \
  /home/tnunamak/applications/llama-tq-mtp/build/bin/llama-server \
    --model "/media/windows/AI Models/LLMs/GGUF/MTP/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf" \
    --port 5052 \
    --host 127.0.0.1 \
    --n-gpu-layers 999 \
    --ctx-size 196608 \
    --parallel 1 \
    --cache-type-k turbo4 \
    --cache-type-v turbo4 \
    --jinja \
    --spec-type mtp \
    --spec-draft-n-max 4 \
    --no-warmup
```

Benchmark:

```bash
./scripts/local/bench_llm_endpoint.py \
  --url http://127.0.0.1:5052/v1/chat/completions \
  --label tq-mtp-qwen27b-196k-turbo4kv \
  --model mtp \
  --runs 3 \
  --warmups 1 \
  --max-tokens 256
```

Opt-in service switch:

```bash
systemctl --user start llama-mtp.service
```

Return to the default:

```bash
systemctl --user start llama-turbo.service
```

## Open Questions

- 218k context looked plausible from the fork's memory projection, but was not
  promoted because the production API needed to be restored before the full load
  and benchmark completed.
- MTP plus mmproj should be treated as unsupported until tested in this exact
  fork. Keep vision on the existing default path.
- Symmetric `turbo4/turbo4` KV is the practical context-gain lever, but it is a
  quality tradeoff. Use the default `q8_0/turbo4` service when answer fidelity
  matters more than the extra 64k context.

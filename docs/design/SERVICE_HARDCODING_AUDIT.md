# Service hardcoding audit — what the registry can't express

Audited 2026-06-12 (proxy.py @ ~19k lines, gateway/, status_endpoint.py,
metrics_sampler.py, firehose_render.py, start.sh). This is the complete map
of runtime behavior keyed to specific service ids instead of the DB service
registry. It is the migration roadmap for making services fully
registry-driven (and the main blocker to this gateway generalizing beyond
this exact machine).

Categories: [config] env-var/port/path constants · [lifecycle] process
management · [routing] request steering · [health] checks/status ·
[protocol] translation quirks · [media] comfy workflow logic.

## tabby (TabbyAPI) — 14 findings

| Where | Cat | What |
|---|---|---|
| proxy.py:315-321 | config | TABBY_HOST/health/load+idle timeout/managed/start_cmd module constants from flat config; default port 5050 |
| proxy.py:319, start.sh:25 | config | TABBY_IDLE_TIMEOUT env var, read once at import |
| proxy.py:371 | config | TABBY_SERVICE default "tabbyapi.service" |
| proxy.py:1226 | config | absolute path /home/tnunamak/applications/tabbyapi/api_tokens.yml (auto-load keys) |
| proxy.py:2039 | config | absolute path tabbyapi/config.yml (model_dir/model discovery) |
| proxy.py:2767-2829, 2931-2944 | lifecycle | start/stop/restart TabbyAPI subprocess in code |
| proxy.py:1038-1128 | lifecycle | swap_tabby_model: Tabby-specific /v1/model/unload + /v1/model/load wire protocol |
| proxy.py:7896, 8006 | lifecycle | idle-unload timer + tabbyapi.service/llama-turbo.service systemd Conflicts= assumption |
| proxy.py:2902-2910 | health | check_tabby_health() named function |
| proxy.py:2041-2062 | health | get_tabby_model_config() live-reads tabby's own config.yml per call |
| proxy.py:18868-18890 | routing | catch-all native route defaults target to TABBY_HOST + tabby restart logic |
| proxy.py:1989, 18636 | routing | x-api-key/x-admin-key injection special-cased on service=="tabby" |
| proxy.py:2592-2612 | routing | vision routing names "tabby" explicitly |
| proxy.py:591-612 | routing | detect_model_backend(): filesystem scan (EXL2 dirs → tabby) |

## kobold (KoboldCpp / llama-turbo) — 9 findings

| Where | Cat | What |
|---|---|---|
| proxy.py:324-327 | config | KOBOLD_HOST default port 5051, health endpoint /api/v1/model, unit koboldcpp.service |
| proxy.py:372 | config | KOBOLD_SERVICE_NAME default in routing config |
| proxy.py:692, 875-963 | lifecycle | swap_kobold_model: writes LLAMA_BEE_MODEL/MMPROJ into systemd 20-model.conf drop-in + daemon-reload + restart |
| proxy.py:763-789 | lifecycle | pgrep llama-server to parse --mmproj from cmdline |
| proxy.py:701-839 | lifecycle | _kobold_state TTL cache + _BACKEND_STATE_STRATEGIES dict keyed to backend type names |
| proxy.py:630 | health | kobold health via KOBOLD_HOST + /api/v1/model |
| proxy.py:2592-2626 | routing | kobold_has_mmproj() gates vision reroute kobold→tabby |
| proxy.py:407 | routing | role model table validates backend in ("kobold","tabby") only |
| proxy.py:18908 | routing | backend_for_host() fallback is literal "kobold" |

## tts (Voxtral) — 7 findings

| Where | Cat | What |
|---|---|---|
| proxy.py:425-427 | config | TTS_HOST default port 8880, TTS_ENABLED, TTS_TIMEOUT |
| proxy.py:2088 | config | default model "mistralai/Voxtral-4B-TTS-2603" |
| proxy.py:583-584, 3049 | routing | /v1/audio/speech pinned to TTS_HOST |
| proxy.py:2961-2967 | health | check_tts_health() named function |
| status_endpoint.py:330, 458-467 | health | probes systemd units voxtral-int4-tts/voxtral-tts/tts.service by name |
| status_endpoint.py:953-954 | health | GPU0_TTS budget key mapped to both voxtral unit names |
| proxy.py:2101-2123 | protocol | VOICE_MAP (OpenAI→Voxtral voices) + DEFAULT_VOICE "neutral_female" |

## stt (Parakeet) — 7 findings

| Where | Cat | What |
|---|---|---|
| proxy.py:431-433 | config | STT_HOST default port 5092, STT_ENABLED, STT_TIMEOUT |
| proxy.py:2094 | config | default model "parakeet-tdt-0.6b-v3" |
| proxy.py:3159 | routing | /v1/audio/transcriptions + /translations pinned to STT_HOST |
| proxy.py:3100-3106 | health | check_stt_health() named function |
| proxy.py:3112-3116 | routing | stt_backend_supports_translation() reads flat config, not registry |
| status_endpoint.py:2333-2351 | health | parakeet probed as named key in status output |
| status_endpoint.py:84 | health | "parakeet-stt" in hardcoded process-name list |

## embeddings — 5 findings

| Where | Cat | What |
|---|---|---|
| proxy.py:437-443 | config | EMBEDDINGS_HOST default port 5061, ENABLED, TIMEOUT (+env var) |
| proxy.py:444 | config | default model "nomic-embed-text-v1.5" |
| proxy.py:18051, 18065, 18127, 18196 | routing | /v1/embeddings path checks route to EMBEDDINGS_HOST |
| proxy.py:2916-2924 | health | check_embeddings_health() named function |
| data/litellm.config.yaml:2-7 | config | gguf filename + port 5061 in generated artifact |

## comfyui — 12 findings

| Where | Cat | What |
|---|---|---|
| proxy.py:448-450 | config | COMFY_HOST default port 7890, ENABLED, TIMEOUT |
| proxy.py:451-461 | config | workflows_dir / video_workflows_dir constants |
| proxy.py:455 | media | COMFY_DEFAULT_MODEL "flux2-klein-4b" |
| proxy.py:505 | media | VIDEO_DEFAULT_WORKFLOW "video-ltx-13b-q8" |
| proxy.py:15240 | media | AUDIO_DEFAULT_WORKFLOW "ace-step-1.5-2b-base" |
| proxy.py:2096, 5874, 12104, 12109 | media | "flux2-klein-4b-edit"/"-inpaint" inline literals in handlers |
| proxy.py:3502-3508 | health | check_comfy_health() via Comfy-specific /system_stats |
| proxy.py:3307-3320 | media | load_comfy_workflow(): workflow JSON selected by filename == model name |
| proxy.py:3819-3945, 4164-4179 | media | VRAM probe/budget tied to ComfyUI PIDs; 3 GiB cold-load threshold; IMAGE_VRAM_RESERVE_BYTES |
| proxy.py:6613-6666 | lifecycle | persistent WebSocket listener to COMFY_HOST |
| status_endpoint.py:59-70, 714-715, 2054-2055 | config | absolute paths: ComfyUI/output, custom_nodes, cache, gpu_budget.sh |
| metrics_sampler.py:134-143 | health | comfy queue probed by name in metrics sampler |

## litellm — 4 findings

| Where | Cat | What |
|---|---|---|
| start.sh:28, 43 | config | port 4011 hardcoded (env default + --port flag) |
| start.sh:37-72 | lifecycle | full subprocess lifecycle (start/config-watch/restart/kill) in shell |
| gateway/litellm_substrate.py | config | DEFAULT_LITELLM_CONFIG_PATH "litellm.config.yaml" |
| gateway/service_registry.py | lifecycle | is_litellm_service_record: unique removal-block + endpoint/protocol lock (intentional) |

## ollama (protocol frontend) — 2 findings

| Where | Cat | What |
|---|---|---|
| proxy.py:1827-1831 | routing | _OLLAMA_DISCOVERY_PUBLIC_PATHS auth-bypass list |
| proxy.py:1822-1824 | config | OLLAMA_COMPAT_DISCOVERY env var |

## cross-service / global — 14 findings

| Where | Cat | What |
|---|---|---|
| proxy.py:329-365 | routing | LLM_BACKEND_HOSTS closed 2-entry dict (kobold, tabby); backend_for_host() can't see new LLM services |
| proxy.py:349-350 | routing | backend_supports() reads flat proxy_config.yml capabilities, NOT the registry DB — gates vision/tts/stt/embeddings decisions |
| proxy.py:546-564 | routing | VISION_MODEL_PATTERNS name-substring list gates vision routing |
| proxy.py:4282, 4510, 4884-4931 | protocol | Qwen3 /no_think hint injected by model-name pattern |
| proxy.py:369-370 | config | MODEL_DIR/GGUF_DIR machine-specific default paths |
| proxy.py:536 | health | HEALTH_CHECK_TIMEOUT global; no per-service health timeout |
| status_endpoint.py:80-85 | health | process-name list: comfyui, llama-turbo, voxtral-int4-tts, voxtral-tts, parakeet-stt, tabbyapi |
| status_endpoint.py:63, 2419 | config | Daisy audit/models absolute paths |
| status_endpoint.py:59 | config | gpu_budget.sh absolute path (+ GPU0_TTS/GPU0_COMFYUI/GPU1_LLM/GPU0_DESKTOP/GPU0_HEADROOM keys) |
| status_endpoint.py:714 | config | _REPO_PATH absolute |
| firehose_render.py:716-991 | health | swap-lock keys "tabby_swap_in_progress"/"kobold_swap_in_progress" |
| firehose_render.py:2514-2531 | health | literal bmap key lookups tabbyapi/tabby, comfyui/comfy |
| data/litellm.config.yaml | config | generated artifact carries gguf filenames + ports (regenerated from registry, but committed) |
| start.sh | config | OPENAI_PROXY_PORT and service env plumbing outside the registry |

## What this means for migration

Rough tiers by tractability:

1. **Connection constants** (TABBY_HOST etc., the ~20 [config] rows): the
   registry already stores host/port/health_endpoint — the module-level
   constants just don't read it. Mechanical migration: derive these from
   `runtime_backends()` at boot.
2. **Capability checks** (`backend_supports`, stt translation, vision
   gates): registry has `capabilities`; switch reads to the registry
   projection. Closed dicts (LLM_BACKEND_HOSTS, process-name lists) become
   registry queries.
3. **Per-service health functions**: one generic `check_service_health(id)`
   driven by registry health_endpoint (+ per-service timeout field, new).
4. **Lifecycle/orchestration** (model swap via systemd drop-ins, tabby
   load/unload protocol, idle timers, pgrep mmproj discovery): genuinely
   service-shaped behavior. Needs a registry `lifecycle` schema (driver:
   none|systemd|subprocess, unit name, swap strategy, idle_timeout) with
   per-driver implementations — the real design work.
5. **Media/workflow logic** (comfy workflow-by-filename, VRAM budgeting,
   default model literals): partly registry fields (defaults), partly its
   own subsystem (workflow catalog).
6. **Machine-specific absolute paths** (tabbyapi configs, ComfyUI output,
   gpu_budget.sh, Daisy paths): config/env injection, prerequisite for any
   non-Tim deployment.

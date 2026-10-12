# Engine-knowledge audit: Azizam at `a71e3e5c`

Date: 2026-10-12. Repository: `~/applications/azizam`, `origin/main` = `a71e3e5c`. Read-only; no code changed, no gateway call.

Owner question: "Confirm that the site doesn't put knowledge of specific models or engines or providers in code, just data, so it is fully extensible."

Owner direction (applied throughout): there is no allowed list and no special treatment for any engine, model or provider. `gateway/engine_quarantine.py` and every consumer of it are debt to delete; the target is zero quarantine tables. The only names allowed in code are:

- the client protocols the gateway serves (OpenAI, Anthropic, Ollama, A1111, MCP request/response shapes and paths);
- the gateway's own internal libraries (LiteLLM).

Even for those, any branch on a specific *upstream* engine or provider is flagged.

## 1. Answer

**No.** Production code holds a large amount of engine, model and provider knowledge:

- About 900 grep-hit lines classify as ENGINE-KNOWLEDGE-IN-CODE (EKIC). They group into **about 95 distinct items** in 25 files.
- Web UI code is clean: 0 EKIC in `web/src` production code.
- The core LLM chat path and the remote-provider path are mostly data-driven.

The debt concentrates in five places:

1. **The image/video/audio-generation stack is a ComfyUI client in code.** About 85 `"comfyui"` literals in `proxy.py`, plus ComfyUI wire paths, graph format and LTX/H3 recipe builders. `/v1/images/*` cannot reach any other engine, including an OpenAI-compatible image server. This is the one **hard blocker** to extensibility.
2. **The kobold/tabby lifecycle.** 11 quarantine tables, 20 consumer sites, named probes (`currently_loaded_kobold_model`, `currently_loaded_tabby_model`, `kobold_has_mmproj`, `_kobold_context_window`) and an engine-keyed probe table `_BACKEND_STATE_STRATEGIES`. A new swap engine id registers and routes, but **re-swaps on every request** because the loaded-model probe is keyed on the ids `kobold`/`tabby`.
3. **Hard-coded service ids and default model ids.** Examples: service ids `"tts"`, `"comfyui"`, `"kobold"`, `"tabby"`. Default ids `flux2-klein-4b`, `qwen-image-2.1-edit`, `Voxtral-4B-TTS-2603`, `parakeet-tdt-0.6b-v3`, `ace-step-1.5-2b-base`, `video-ltx-13b-q8`, `tts-1`, `whisper-1`, `chatterbox`. Branch: `if engine_id == "omnivoice"`.
4. **Status, metrics and firehose code** (`status_endpoint.py`, `firehose_render.py`, `metrics_sampler.py`) is written for llama.cpp, ComfyUI, Voxtral, Parakeet and Tabby. It uses fixed ports, `/props`, `pgrep llama-server` and CLI-flag parsing.
5. **Protocol code that branches on upstream engines** (owner's specific flag):
   - `responses_translator.py:613-660` injects Qwen's `/no_think` and `chat_template_kwargs.enable_thinking=false` for every backend.
   - `transforms.py` has a Qwen-named thinking key.
   - `toolcall_detection.py` has a compiled marker→model-family table and `vllm` hint matching.
   - The OpenAI model-name prefix table in `proxy.py:2829-2949`.

The CI guard (`tests/test_engine_knowledge_boundary.py`) gives false comfort:

- It scans only `proxy.py` and `gateway/**`, not `status_endpoint.py`, `firehose_render.py`, `responses_translator.py` or `metrics_sampler.py`.
- Its name denylist lacks `comfy`, `voxtral`, `omnivoice`, `chatterbox`, `kokoro`, `parakeet`, `whisper`, `flux`, `ltx` and model ids.
- It only catches literals inside `if`-style branch conditions. `"comfyui"` as an argument value and `engine_id == "omnivoice"` (the variable name is not `engine`/`backend`) both pass.

Walk-through result:

| Case | Code-free today? |
|---|---|
| New OpenAI-compatible TTS server with its own voices | **Yes**, but through the API plus a `proxy_config.yml` edit plus `safe-restart`. Not through the UI. |
| New hosted OpenAI-compatible LLM provider | **Yes**, through the API or a catalog YAML file. Chat only. |
| New local swap LLM engine under a new id | Registers, but re-swaps on every request. Works only by re-using the id `kobold`/`tabby`. |
| New image engine (OpenAI-compatible or another runtime) | **No: hard blocker.** |

## 2. Method and coverage

| Source | Scope | Result used |
|---|---|---|
| 4 dot audits (`azizam-engine-knowledge-{gw1,gw2,web1,web2}-a71e3e5-r0`, all finished) | gw1: 61 gateway files; gw2: 61 gateway files; web1/web2: 61 web files each | gw1 22 EKIC groups; gw2 11 EKIC groups; web1 0; web2 0. Spot-verified below. |
| 5 subagents over `proxy.py` (lines 1-6022, 6023-12031, 12032-18011, 18012-24006, 24007-29418) | proxy.py (not readable by dots) | about 90 EKIC rows |
| 1 subagent: root `.py` files plus 31 gateway/JSON files the dot scopes omitted (`status_endpoint`, `firehose_*`, `metrics_*`, `responses_*`, `local_capabilities`, `mcp_server`, `media_*`, `model_files`, `setup_*`, ...) | the gap left by dot scope lists | about 27 EKIC rows |
| 1 subagent: `engine_quarantine.py`, `video_*`, `voice*`, `workflow*` plus every quarantine consumer | the gap left by dot scope lists | 11 tables, 20 consumer sites, 4 extra engine-keyed tables |
| 1 subagent: 37 web/src files the dot scopes omitted | gap | 0 EKIC |
| 1 subagent: end-to-end "add an engine" walk-through | admin API/UI/config/routing | section 7 |
| Direct verification by me | every quarantine consumer (grep matches the list exactly); `proxy.py:1327-1350`, `2824-2949`, `4027`, `4148-4154`, `23929-23967`, `1139-1168`; `responses_translator.py:610-660`; `toolcall_detection.py:52-90`; `local_capabilities.py:443,471`; `lifecycle_drivers.py:836-850`; guard scope `_core_files()`; live `admin.db` service registry (read-only) | confirmed |

Dot scope lists stop part-way through the alphabet, apparently truncated. They skipped about 45 gateway files and about 60 web files. The extra subagents covered that gap. Tests and docs are excluded throughout.

Counting unit: one physical source line with at least one matching name. Class counts are approximate (±10%) because agents grouped repetitive lines. The owner's earlier quick-grep figures (e.g. litellm 175/22) used a different filter. My raw counts over the 316 production files (`proxy.py`, root `.py` modules, `gateway/*.py`, `web/src` non-test), comments included, are:

`comfy 775/37 · litellm 589/24 · llama 349/19 · ollama 275/9 · tabby 148/11 · kobold 116/5 · mmproj 101/7 · ltx 61/8 · voxtral 47/3 · gguf 37/10 · qwen 25/8 · sora 23/4 · vllm 21/6 · flux 16/3 · parakeet 16/3 · tts-1 9/4 · omnivoice 6/3 · whisper 4/2 · chatterbox 3/2 · openrouter 1/1 · supertonic 1/1 · kokoro 0`  (lines/files)

## 3. Classified table

### 3.1 Per source (hit lines)

| Source | CONTRACT | DEPENDENCY | COMMENT/DOC | DATA | EKIC | Notes |
|---|---:|---:|---:|---:|---:|---|
| proxy.py 1-6022 | ~75 | ~45 | ~110 | ~85 | ~190 | kobold/tabby lifecycle, defaults, ComfyUI config |
| proxy.py 6023-12031 | ~33 | 0 | ~130 | ~10 | ~205 | ComfyUI client, ideogram4, LTX defaults |
| proxy.py 12032-18011 | ~25 | ~16 | ~30 | ~10 | ~95 | ComfyUI video jobs, Tabby unload loop, showcase probes |
| proxy.py 18012-24006 | ~50 | 0 | ~22 | ~15 | ~95 | ComfyUI image/audio, Flux2 fallbacks, context probes |
| proxy.py 24007-end | 341 | 211 | 3 | 14 | 28 | Anthropic/Ollama/OpenAI frontends, LiteLLM hop |
| root .py + gateway gap A (31 files) | ~60 | 1 | ~85 | ~5 | ~165 | status_endpoint, firehose_render, responses_translator, local_capabilities |
| gateway gap B (16 files incl. quarantine) | ~22 | 0 | ~30 | 1 | ~50 | quarantine tables, video_extend_h3, ComfyUI history parser |
| dot gw1 (61 gateway files) | 49 | 171 | 167 | 2 | 29 | ComfyUI graph/object_info, lifecycle_drivers Tabby defaults, identity models |
| dot gw2 (61 gateway files) | 145 | 72 | 195 | 31 | 43 | toolcall markers, Qwen thinking key, LTX recipe, Typesafe provider path |
| web/src (web1 + web2 + gap) | 47 | 3 | ~29 | 4 | **0** | test-only lines excluded |
| **Total (approx.)** | **~850** | **~520** | **~800** | **~180** | **~900** | about 95 distinct EKIC items |

### 3.2 Per name: where each name is legitimate and where it is debt

| Name | Legitimate uses (class) | EKIC uses |
|---|---|---|
| litellm | Internal LLM substrate: config compile, virtual keys, sidecar, cost header (DEPENDENCY) | `openai/` prefix strip `proxy.py:26897` (DEPENDENCY convention, OK); none keyed on an upstream engine |
| ollama | `/ollama/api/*` frontend, `gateway/protocols/ollama.py` (CONTRACT) | `resource_admission_http.py:147,153` `protocol == "ollama"` is the served envelope (CONTRACT), but it is today an **allowlist entry** (see 6); `PROVIDER_PRESETS` row (DATA in code; see 4.2); `_BACKEND_STATE_STRATEGIES` comment |
| openai / anthropic / a1111 / mcp | Served protocols (CONTRACT) | OpenAI model-name prefix inference `proxy.py:2829-2949` (E-83); a1111 ComfyUI sampler table `a1111_api.py:50-54,158-162` and `seedvr2` error `:81-85,457` (E-60, E-61); `config_sources.py:97-98,504-509` provider-name substring labels (E-87) |
| comfy / comfyui | none: ComfyUI is not a served protocol | All of it: section 5.C |
| kobold, tabby, llama, gguf, mmproj | Comments | Section 4 (quarantine) and 5.A/5.B |
| vllm | Preset row (DATA in code) | `toolcall_detection.py:81-83,188` hint match; `_BACKEND_STATE_STRATEGIES["vllm"]`; `status_endpoint.py` ExecStart sniff |
| qwen | Comments | `responses_translator.py:613-660`; `transforms.py:575-594`; `proxy.py:6755`; `identity_scene.py:39` (MAX_IMAGE_SLOTS justified by Qwen-Image); `proxy.py:24526` description; defaults `qwen-image-2.1-edit*` |
| voxtral, parakeet, chatterbox, omnivoice, kokoro, supertonic, whisper | Voice/engine rows in `proxy_config.yml` (DATA) | `proxy.py:2814,2824,4027,4099,4150`; `status_endpoint.py:523-580,2540-2620`; `firehose_render.py` cards |
| flux, ltx, sora, ace-step, minimax/H3, ideogram | `sora-*` as public OpenAI video names (CONTRACT) | Defaults and fallbacks, LTX 8n+1 rule, H3/LTX recipe builders, ideogram4 prompt rewrite (5.C) |
| tts-1, whisper-1, dall-e, gpt-image, alloy | Public OpenAI names a client may send (CONTRACT); `dall-e-` url default `proxy.py:7658` (CONTRACT) | Used as **fallback defaults** when `model` is absent (`proxy.py:4339,4418,5067,17908`) and as placeholder voice `alloy` (`proxy.py:4148`) |
| openrouter, typesafe/systemone | Provider rows (DATA) | `providers.py:775-787,827-837,848-881,958-967` named Typesafe estimator, path and auth (E-86) |

## 4. The quarantine: tables, consumers and engine-neutral replacements

All 11 tables live in `gateway/engine_quarantine.py:53-94`. Only `proxy.py` imports it (`proxy.py:184`). The guard pins the tables at `tests/test_engine_knowledge_boundary.py:301-312`.

Live registry (read-only from `data/admin.db` `operator_settings.service_registry`):

| Service | Enabled | Driver | State type | Models | Other |
|---|---|---|---|---|---|
| `kobold` (llama-bee / llama.cpp) | yes | `systemd` | `llama_cpp` | 9, each with `context_window` | `precheck: gguf_path`, env keys `model: LLAMA_BEE_MODEL`, `aux: LLAMA_BEE_MMPROJ` |
| `tabby` | **no** | `http-load-unload` | none | 0 | `load_body_params: tabby-config`; `AZIZAM_TABBYAPI_DIR` is set in `.env.local` |

So six tables serve only a disabled service.

### 4.1 Table → field → seed → consumers

| Table (members) | Engine-neutral replacement field | Seed rows (plain data) | Consumers to rewrite (proxy.py) | Effort |
|---|---|---|---|---|
| `LOCAL_SWAP_BACKENDS` {kobold, tabby} | None new: derive from `lifecycle_drivers.supports_swap(driver_for_service(id))` (exists, `lifecycle_drivers.py:324`) | none; both rows already have swap-capable drivers | `:705` alias (delete); `:723` and `:24233` "known backends" union (delete: the rows are already in `config["backends"]`); `:24244` role shape: use `supports_swap(...) or role_def.path`; `:6721` background-removal LLM gate: use the normal chat routing (`host_for_capability("chat")`) instead of "active local-swap backend" | S |
| `_HAS_DEFAULT_MODEL_FALLBACK` {tabby} | existing `config.default_model` | `tabby.default_model: <TABBY_DEFAULT_MODEL or model.model_name from the Tabby config.yml>`; value to read at migration time (UNVERIFIED) | `:3162`, `:3171`; also delete the global `DEFAULT_MODEL`/`MODEL_DIR` from `get_tabby_model_config()` `:2749-2775` | S |
| `_ACTIVATION_FROM_UPSTREAM_ID` {tabby} | `lifecycle.driver_config.activation.model_source: activation_model \| upstream_model` (default `activation_model`) | `tabby: {model_source: upstream_model}` | `:3230` | S |
| `_ACTIVATION_REQUIRES_ABS_PATH` {kobold} | `lifecycle.driver_config.activation.require_absolute_path: bool` (clears a relative value, same as today; distinct from the existing `precheck: gguf_path`, which rejects) | `kobold: {require_absolute_path: true}` | `:3232` | S |
| `_LOADED_MODEL_IS_PATH` {kobold} and `_LOADED_MODEL_IS_HTTP_ID` {tabby} | `lifecycle.driver_config.loaded_state` with two engine-neutral sources. **`activation_record`**: the driver reads back the activation it wrote itself (systemd drop-in env keys `model`/`aux`); no engine probe. **`http`**: `{endpoint, model_field (dot path), compare: path\|id, auth_ref}` using the existing `auth_inject`/`secret_env` mechanism. Also replaces the code table `_BACKEND_STATE_STRATEGIES` (`:1139-1168`) and `state_type` | `kobold: {source: activation_record}` (or `{source: http, endpoint: /props, model_field: model_path, compare: path}` for byte-parity); `tabby: {source: http, endpoint: /v1/model, model_field: id, compare: id, auth_ref: x-api-key/TABBY_API_KEY}` | `:1335`, `:1337` (`_currently_loaded_model_for`); `:3527`, `:3533`; `:13545`; `:13575` plus literal `backend="kobold"` `:13570`; delete `currently_loaded_kobold_model/mmproj`, `currently_loaded_tabby_model`, `get_tabby_loaded_model_id`, `get_kobold_model_id`, `_kobold_state`, `_seed_kobold_state`, `effective_llm_model_for_host` host comparisons (`:1121-1339`, `:3473-3506`) | M |
| `_HAS_AUX_MMPROJ` {kobold} | `loaded_state` above returns `aux` too (`activation_record` reads the `aux` env key; `http` gains optional `aux_field`). Deletes the `pgrep llama-server --mmproj` probe `:1185-1213`. Rename record field `activation_mmproj` → `activation_aux` with read-fallback for one release | `kobold: loaded_state.source: activation_record` (covers aux) | `:1349` (`_currently_loaded_aux_for`); `:1469` cache reset | M |
| `_VISION_VIA_AUX` {kobold} | Model-record metadata `aux_grants_input_modalities: [image]`; vision = the model declares it **and** the loaded aux equals the record's `activation_aux` | each kobold model row with an `activation_mmproj`: `aux_grants_input_modalities: [image]` (they already declare `input_modalities: [text, image]`) | `:3372` (`kobold_has_mmproj()`), `:3384` | S-M |
| `_ROLES_MAY_BE_UNPINNED` {tabby} | `config.roles_resolve_to_loaded_model: bool` | `tabby: true` | `:13497` | S |
| `_CONTEXT_WINDOW_FROM_MODEL_PARAMS` {tabby} and `_CONTEXT_WINDOW_FROM_LIVE_PROBE` {kobold} | `config.context_window_source: {order: [declared, live, load_params], live: {endpoint, items_path}, load_params_field}`. `declared` = existing `models[].context_window` | `kobold: {order: [declared, live], live: {endpoint: /v1/models, items_path: data}}` (all 9 live models already declare a window, so the probe is a fallback only); `tabby: {order: [declared, load_params], load_params_field: max_seq_len}` | `:23932` (`get_tabby_model_config()`), `:24029` (`_kobold_context_window` `:23941-23967`, cache key `"kobold"`) | M |
| (not a table, same debt) `"tabby-config"` params provider `proxy.py:2777-2789`, `lifecycle_drivers.py:836-850` | `lifecycle.driver_config.load_body: {…}` literal data, or a generic `load_body_from_file: {path, select}` provider | `tabby.load_body: {max_seq_len, cache_size, cache_mode, …}` copied from Tabby's config.yml at migration (UNVERIFIED values) | `PARAMS_PROVIDERS` registry: delete | S |

After these rows land: `engine_quarantine.py` is deleted; `_QUARANTINE_TABLES` / `_QUARANTINE_PREDICATES` in the guard become a test that the module does not exist.

**Simplest alternative for the tabby half (recommended to ask the owner):** `tabby` is disabled. Deleting Tabby support outright removes 6 tables, the Tabby unload loop (`proxy.py:12767-12845`), `get_tabby_model_config` and `TABBY_*` globals. An operator who wants it back authors the rows above. This is the cheaper path if the owner does not run Tabby.

### 4.2 Duplicated engine-keyed tables outside the quarantine (same treatment)

| Location | What | Replacement | Effort |
|---|---|---|---|
| `proxy.py:1139-1168` `_BACKEND_STATE_STRATEGIES` {llama_cpp, tabbyapi, vllm, openai_compatible} | Probe endpoints per engine | Becomes `loaded_state.http` seed rows (4.1). Delete the table and `state_type` | M (with 4.1) |
| `proxy.py:1225` default `state_type="llama_cpp"` | Engine default | gone with the above | S |
| `gateway/service_registry.py:904-964` `PROVIDER_PRESETS` (vllm, ollama, tabbyapi, lmstudio, comfyui) and `:966` `_CAPABILITY_CHOICES` | Preset rows stored as a Python tuple | Move to `presets/*.yml` data files (like `providers/catalog/*.yml`), loaded at runtime; add speech/STT/image presets as data | S |
| `gateway/service_registry.py:978-985,1243-1257` | ComfyUI-keyed field schema (`workflows_dir`, `video_workflows_dir`, `default_model`) | Field schema declared on the preset row | S |
| `gateway/toolcall_detection.py:52-65,81-83,188` | Marker→family table (hermes_or_qwen, mistral, llama3, gemma, command_r) and `vllm` substring hint | `config.toolcall_markers: [{marker, family}]` per service (seed the six rows on kobold); delete hint matching | M |
| `gateway/local_capabilities.py:443,471` | `"comfyui" if "comfyui" in backends else "comfyui"` (no-op) | capability built over every service with image/video/audio caps | S (with 5.C) |

## 5. ENGINE-KNOWLEDGE-IN-CODE list

The "Data replacement" column states the operator-owned data that would replace each item. Effort: S < 1 day, M 1-3 days, L > 3 days. Line numbers come from the agents' reads at `a71e3e5c`; items marked ✔ I re-read myself.

### 5.A Kobold/Tabby/llama.cpp LLM lifecycle (beyond the 4.1 consumers)

| # | file:line | What it does | Data replacement | Effort |
|---|---|---|---|---|
| E-1 | proxy.py:446-464, 945-953, 979-980, 1027 | `TABBY_HOST`, `KOBOLD_HOST`, health paths from `backends.tabby/kobold` by name, default ports 5050/5051 | Look up by service id from `LLM_BACKEND_HOSTS`; no named globals | M |
| E-2 | proxy.py:938-1000 `_derive_backend_constants` | Hard-reads ids `tabby, kobold, tts, stt, embeddings, comfyui` | Resolve per capability from the registry | M |
| E-3 | proxy.py:680-682, 1086-1088 | `tabbyapi.service`, `koboldcpp.service`, `20-model.conf`, `GGUF_DIR` | Driver config already holds unit and drop-in; delete (probably dead, UNVERIFIED) | S |
| E-4 | proxy.py:1528-1551 | `TABBY_ADMIN_KEY`, `TABBY_API_KEY`, `AZIZAM_TABBYAPI_DIR`, `api_tokens.yml` | Per-service `secret_env` (exists) | M |
| E-5 | proxy.py:12767-12845 | Tabby `GET /v1/model`, `POST /v1/model/unload`, idle-unload loop on `TABBY_IDLE_TIMEOUT` | Generic `idle_unload_seconds` + driver `unload` (HttpLoadUnloadDriver already has it) | M |
| E-6 | proxy.py:1053 | `/v1/model` in `NO_ACTIVITY_PATHS` | Drop it, or make the path list configuration | S |
| E-7 | proxy.py:913-922, 1036-1048 | Vision inferred from model-name substrings (`vl`, `pixtral`, `llava`, `cogvlm`, `internvl`) | Model metadata `input_modalities` only | M |
| E-8 | proxy.py:24687-24700 | Drops `/…gguf` ids from every upstream catalog (comment: kobold quirk) | `config.catalog_hide_ids: <regex>` on kobold | S |
| E-9 | proxy.py:26931-26940; gateway/model_files.py:17-25 | `mmproj`/`activation_mmproj` vocabulary in generic activation | Rename to `activation_aux` with read-fallback | S |
| E-10 | proxy.py:13824-13830 | "llm" capability enabled iff `kobold_cfg` or `tabby_cfg` exists | Any service with chat capability | S |
| E-11 | proxy.py:14223-14241 | Showcase probes `{KOBOLD_HOST}/slots` (`is_processing`) and `{COMFY_HOST}/queue` | `config.status_probe: {endpoint, fields}` per service | M |
| E-12 | proxy.py:17358-17363 | Reload response keys `tabby`/`kobold`/`comfyui` | `{service_id: host}` | S |
| E-13 | gateway/lifecycle_drivers.py:1451-1706 | HttpLoadUnloadDriver defaults are Tabby-shaped: `x-admin-key`, `/v1/model/load`, `/unload`, `model_name`, `"finished"` marker, `.id`, `x-api-key`. `activation_model_key` is ignored in swap and load payload | Required `driver_config` fields; no defaults; honor `activation_model_key` everywhere | M |
| E-14 | gateway/lifecycle_drivers.py:1015, 1021 | Precheck message prints `ensure_target(kobold)` and mmproj wording | Interpolate service id and aux role | S |
| E-15 | gateway/backends/llm_execution.py:90-118 | Global request allowlist includes llama.cpp sampler fields (`mirostat`, `dry_*`, `xtc_*`) for every target | Per-service `extra_request_fields` (security deny list stays code) | M (UNVERIFIED intent) |
| E-16 | proxy.py:24526 | Alias description "Qwen thinking off" | Text "reasoning off" | S |

### 5.B Speech, STT and voice

| # | file:line | What it does | Data replacement | Effort |
|---|---|---|---|---|
| E-20 ✔ | proxy.py:4150 | `if engine_id == "omnivoice":` fetch a reference transcript and send `ref_text` | Speech engine row `custom_voice.reference_transcript: true` | S |
| E-21 ✔ | proxy.py:4148 | `body_json["voice"] = "alloy"` placeholder for custom voices | Engine row `custom_voice.placeholder_voice` (or omit `voice`) | S |
| E-22 ✔ | proxy.py:4027 | Quality-probe engine defaults to `"chatterbox"` | Required `voices.quality_engine`; unset gives an honest "not configured" (doc P2-5) | S |
| E-23 ✔ | proxy.py:4099 | STT fallback `parakeet-tdt-0.6b-v3` | `stt.default_model` required | S |
| E-24 ✔ | proxy.py:2814, 2824 | Default speech `mistralai/Voxtral-4B-TTS-2603`, STT `parakeet-tdt-0.6b-v3` | `compat.defaults.*` only; no literal | S |
| E-25 ✔ | proxy.py:4339, 4418, 5067, 17908 | Missing `model` falls back to `"tts-1"`/`"whisper-1"` | `default_model_for_operation(op)` from config | S |
| E-26 | proxy.py:751-752, 4363-4382, 3907, 3936, 17869 | Service id `"tts"` is a hard gate: disabling it returns 503 for all speech; it supplies the default voice list | Speech operation resolves through `model_aliases.speech` only; `TTS_ENABLED` = "any speech service enabled" | M |
| E-27 | proxy.py:4494-4505 | Speech URL built as `http://host:port/v1/audio/speech` (drops scheme and base_path) | Use the service `base_url` + path (like chat) | S |
| E-28 | proxy.py:4361-4700 | Request transforms are not applied on the speech route | Run `config.transforms` on speech like chat | M |
| E-29 | proxy.py:17871 | `/v1/audio/voices` `engines` map skips engines without `voice_map` | List every engine with `voices` | S |
| E-30 | status_endpoint.py:523-580, 670-680, 2540-2620 | Voxtral/vLLM unit sniffing; Voxtral port 8001 and Parakeet port 5092 health; `_OPENAI_VOICE_NAMES` classes | Loop over service rows with `health_endpoint`; voice source from voice rows | M |

### 5.C ComfyUI generation stack (not a served protocol, so debt)

The gateway serves OpenAI images/videos/audio and A1111 to clients; ComfyUI is an **upstream** engine. Today every image, video and music route ends in ComfyUI-specific code.

| # | file:line | What it does | Data replacement | Effort |
|---|---|---|---|---|
| E-40 ✔ | proxy.py: about 85 `"comfyui"` literals (e.g. 8043, 8058, 8149, 8480, 8635, 9119, 10633, 11846, 12262, 12660, 13368, 13735, 14811, 18216, 21626, 22194, 23421, 23564), plus `COMFY_ENABLED`/`COMFY_HOST` | Routes, admission, receipts, queues and labels pick the backend by literal id | Resolve the service from the model's manifest binding (`bindings.<service_id>`) or by capability | M |
| E-41 | proxy.py:8703, 10517, 19030-19038, 19075, 19088, 19314, 19496, 19656, 22907, 23373-23376, 24119-24121, 24340-24353; video_extensions.py:179,198; comfy_workflow_catalog.py:16-17 | Manifest key `bindings.comfyui` | `bindings.<service_id>` with `service.job_api` (E-42) deciding how to run it | S-M |
| E-42 | proxy.py:5450-5612, 5917-5970, 7217-7529, 9658-9923, 10067-10125, 10425-10646, 11331, 11661, 12032-12515, 18560-18730, 18900-18999, 19565, 22983-23062, 23649-23783; video_media_jobs.py:145-175 | ComfyUI wire client: `/prompt`, `/history`, `/queue`, `/view`, `/upload/image`, `/api/jobs/{id}/cancel`, `/free`, `/system_stats`, `/ws`, plus the response shapes (`execution_error`, `outputs[node].images/videos`) | Generic "HTTP graph-job" runner driven by service `config.job_api: {submit, status, queue, outputs, fetch, upload, cancel, free, progress_ws, health}` (path templates + JSON paths); seed the ComfyUI values as a preset row | L |
| E-43 | proxy.py:22629-22640, 8036 | `/v1/images/*` calls `proxy_comfy_request` unconditionally; no passthrough to an OpenAI-compatible image upstream | Dispatch by resolved service `protocol`: `openai-images` passthrough (the gateway already serves that shape) or `job_api` | M |
| E-44 | local_capabilities.py:379-480 | Image/video/audio capabilities built only from `backends["comfyui"]` | Iterate every service with image/video/audio caps | S |
| E-45 | stable_aliases.py:55, 318-345 | Engine tiers (ordered fallback) allowed only for speech/STT/rerank/decisions, not images/videos | Allow `engines` for every operation | M |
| E-46 | comfy_object_info.py:60-80; comfy_ui_graph.py (whole); comfy_workflow_catalog.py:19-85; comfyui_workflow_validation.py:8-36 | `/object_info` schema lookup, ComfyUI UI-graph (v0.4) serializer, dynamic-input types, `EXTRA_PNGINFO`, API-graph validation | Store the UI twin as a workflow artifact (data) and validate graphs by an operator-supplied JSON schema; delete the derivation | L |
| E-47 | proxy.py:6023-6300, 7696-7801; status_endpoint.py:1431-1545 | Finds ComfyUI by `/proc` cmdline `main.py` + cwd `ComfyUI`; per-process VRAM; "flux marginal VRAM" | `config.process_match` (or unit) on the service; generic per-service VRAM | M |
| E-48 | proxy.py:7801, 19715-19716 | Public response header `X-ComfyUI-Models-Freed` | `X-Azizam-Models-Freed` (keep the old one one release) | S |
| E-49 | proxy.py:782, 816, 822, 852, 2826, 20416, 22404 | Defaults `flux2-klein-4b`, `qwen-image-2.1-edit(-multiref)`, `video-ltx-13b-q8` | Required config, or the first catalog entry with that operation | S |
| E-50 | proxy.py:18812-18830 | Offset-inpaint falls back to `flux2-klein-4b(-inpaint)`; error text names Flux2 | Manifest `base_model`/`inpaint_model` (exist); fail if absent | S |
| E-51 | proxy.py:18819-18870, 8926-8935 | Legacy writes into graph nodes (`_meta.prompt_node`, `size_node`, hard-coded Flux2 node ids `"47"`, `"48"`) | Manifest `map` only | S-M |
| E-52 | proxy.py:9219-9222, 19099-19115 | `-edit`/`-inpaint` name suffixes decide edit/inpaint workflows | Manifest `operations`/`edit_variant` fields | M |
| E-53 | proxy.py:6573-6617, 8091-8099, 6458-6560 | `startswith("ideogram4")` rewrites the prompt via a chat call hard-wired to `{KOBOLD_HOST}`; Ideogram validator | Manifest `prompt_preprocess: {template, role, validator}` resolved by chat routing | M |
| E-54 | proxy.py:10015-10050, 11148-11252 | LTX 8n+1 frame defaults; reason code `ltx_latent_8np1_stride` when step == 8 | Required manifest `length_step/offset`; neutral reason code | S |
| E-55 | proxy.py:10995, 11897-11899; video_extend_h3.py:252 | Error text lists `video-long`, `ltx-2.5-long`, `minimax-h3`; fallback `video-long` | Build from manifests having `chain_workflow` | S |
| E-56 | gateway/video_extend.py:25-372 (LTX 2.5 chain) and video_extend_h3.py:44-230 (MiniMax H3) | Python graph builders with hard-coded weights, samplers, frame arithmetic, node classes; registry `ltx25_chain`/`h3_chain` | Template graph JSON + manifest `chain: {frame_step, frame_offset, context_windows, fps, segment_template}` executed by one generic chain planner | L |
| E-57 | gateway/video_subject.py:101-127 | Fixed ComfyUI subgraph spliced into workflows | Subgraph template as workflow data | M |
| E-58 | gateway/identity_scene.py:39, 291-367, 750-758, 817-824; proxy.py:14850-14893 | `MAX_IMAGE_SLOTS=10` justified by Qwen-Image; `<imageN>` prompt build; writes `graph[node]["inputs"]["image"]` | Manifest slot count, prompt template, binding paths | M |
| E-59 | gateway/likeness.py:27-40, 154, 220-282; identity_scene.py:43, 225-279, 626, 882-890; identity_api.py:72 | Recognizers `buffalo_l`/`antelopev2`; ArcFace-specific infant threshold and yaw rules | Face-service config / pipeline seed for model ids and calibration | M |
| E-60 | gateway/a1111_api.py:50-54, 158-162 | A1111 frontend converts ComfyUI sampler spellings via a code table (the upstream leaks into the served protocol) | value-map row on the workflow binding | S |
| E-61 | gateway/a1111_api.py:81-85, 457 | Error prescribes the `seedvr2-upscale` workflow | Catalog lookup by capability, or omit | S |
| E-62 | proxy.py:23268 | Audio default `ace-step-1.5-2b-base` | Config only | S |
| E-63 | gateway/workflows_catalog.py:251, 255 | Regex hides `comfyui`, `gguf`, `safetensors`, `ckpt` from user copy | Settings word list | S |
| E-64 | status_endpoint.py:779-850, 1008-1140, 1387-1417, 1570-1612, 2373-2490, 2972-2975, 3720-3736; metrics_sampler.py:186, 292-310 | Port 7890, `/queue`, `/system_stats`, ComfyUI dir layout, `GPU0_COMFYUI` budget key, LTX node-class capability sniffing, `comfy_queue_*` metric names | Per-service `status_probe`; manifest capability flags; metric `queue_running{service}` (migration of stored series) | M-L |

### 5.D Status page, firehose and metrics (llama.cpp and the others)

| # | file:line | What it does | Data replacement | Effort |
|---|---|---|---|---|
| E-70 | status_endpoint.py:405-520, 582-596, 3989-4000 (two near-copies) | `127.0.0.1:5051/props`; `pgrep -fa llama-server`; parses `--model --mmproj --ctx-size --tensor-split --parallel` | `config.status_probe: {endpoint, field_map}`; dedupe; drop argv parsing | M |
| E-71 | status_endpoint.py:2338-2340, 2946-2954, 3848 | `SERVICE_PATTERNS` (`llama-server comfyui voxtral parakeet tabby …`); `tabby_swap_lock`/`kobold_swap_lock` | From service rows; generic `swap_locks[service_id]` | S |
| E-72 | firehose_render.py:310-410, 586-601, 714-990, 1276, 1406-1615, 2029-2042, 2464-2652 | Engine-named cards and alerts (`llama_crash`, Tabby/Kobold swap, Voxtral/Parakeet cards, warmth grid); curl recipes with `flux2-klein-4b`, `voxtral-4b-tts`, `nova`, `ltx-2.5`, `ai.vivid.fish` | Render `probes[service_id]` generically; recipes from catalog | M |
| E-73 | metrics_sampler.py:292-310 | `llama_slots_*`, `comfy_queue_*` stored series | Labelled generic series; dual-write during migration | M |

### 5.E Protocol and dependency code that branches on an upstream engine or provider (owner's flag)

| # | file:line | Protocol | Upstream knowledge | Data replacement | Effort |
|---|---|---|---|---|---|
| E-80 ✔ | responses_translator.py:613-660 | OpenAI Responses → Chat translation | `reasoning.effort` none/minimal injects Qwen `/no_think` into the system prompt and sets llama.cpp `chat_template_kwargs.enable_thinking=false` **for every backend** | Translator emits only the protocol's `reasoning_effort`; per-service transform rows (`message-inject`, `default-params`) do the rest. The existing `reasoning-effort-final-chat` attachment is the home | M |
| E-81 | gateway/transforms.py:575-594, 670-672 | Reasoning transform | `QWEN3_ENABLE_THINKING_KEY` thinking-off kwarg; compiled budgets (doc P2-2) | `config.reasoning_wire` on the service (P2-2 plan) | S-M |
| E-82 | proxy.py:6755, 6591-6600 | Internal classifier call | Same Qwen kwarg on the background-removal request | Use the target service's transforms | S |
| E-83 ✔ | proxy.py:2829-2834, 2909-2949 | OpenAI model-name handling | Static `dall-e-*`/`tts-1`/`whisper-1` categories and prefix rules (`gpt-`, `o1`, `sora`, `-tts`, `whisper`, `embedding` …) applied to **any** model id | Seed rows in `compat.aliases` (already consulted first); delete the prefix heuristics. Accepting the names is CONTRACT; the table is data | M |
| E-84 | gateway/toolcall_detection.py:52-65 | Chat tool-call advisory | Model-family markers | `config.toolcall_markers` (4.2) | M |
| E-85 | proxy.py:27010-27022 | same | `preset`/service-id hint (`vllm`) | delete with E-84 | S |
| E-86 | gateway/providers.py:775-787, 827-837, 848-881, 958-967 | Provider execution | Typesafe/SystemOne: named estimator (`output_tokens=0`), `/systemone` vs `/v1/systemone` path, forced Bearer, model-prefix adapter | Provider catalog row: `paths`, `auth`, `estimator: {counted_fields, output_policy}` | M |
| E-87 | gateway/config_sources.py:97-98, 114-127, 504-509 | Env inventory | `OPENAI`/`ANTHROPIC` substring labels; known env names `COMFY_*`, `TABBY_IDLE_TIMEOUT` | Env fields declared on preset/provider rows | S |
| E-88 | gateway/decision_routing.py:88-109 | Decisions API | `jev-*` prefix fabricates `jev-latest/preview` | Explicit alias rows | S |
| E-89 | gateway/showcase.py:192-200; proxy.py:14241, 24851 | Showcase/status payload | `comfyui`, `comfy_queue` response keys | Generic `engines[]`; keep old key one release | S |
| OK | proxy.py:26897 (`openai/` prefix strip), LiteLLM cost header, `/ollama/api/*`, `/anthropic/v1/*`, A1111 routes, MCP | — | none | stays code | — |

### 5.F Instance/deployment literals (not engines, same rule)

| # | file:line | What | Replacement | Effort |
|---|---|---|---|---|
| E-90 | proxy.py:10153; firehose_render.py:2608-2639 | `https://ai.vivid.fish`, `https://openai.vivid.fish` | `instance_presentation.base_url` (exists in admin.db) | S |

## 6. Guard: from "frozen quarantine + allowlist" to "empty, no allowlist"

The current guard (`tests/test_engine_knowledge_boundary.py`):

| Element | Today | Target |
|---|---|---|
| `_ENGINE_BRANCH_ALLOWLIST` (`:117-137`) | 2 entries, both `protocol == 'ollama'` in `resource_admission_http.py:147,153` | **Empty dict, then deleted.** Move the Ollama error envelope into `gateway/protocols/ollama.py` as a method of the served frontend (`frontend.error_envelope(body)`), so admission dispatches on the frontend object, not on a literal. |
| `_ENGINE_CLASS_ALLOWLIST`, `_ENGINE_FLAG_ALLOWLIST` | already empty | delete the dicts (no allowlist mechanism at all) |
| `_QUARANTINE_TABLES` / `_QUARANTINE_PREDICATES` (`:301-370`) | freeze 11 tables | each slice deletes its entry; last slice replaces them with `assert not (gateway/engine_quarantine.py).exists()` |
| `_SEMANTIC_POLICY_ANCHORS` (`:385-401`) | anchors P2-2/5/8/9/10 code | remove each anchor when its item migrates; P2-2 = E-80/E-81 |
| Scan scope `_core_files()` (`:60-62`) | `proxy.py` + `gateway/**` | **all production Python**: add root `*.py` (`status_endpoint`, `firehose_*`, `metrics_*`, `responses_*`). Scope web stays on the existing UI guard. |
| `_ENGINE_NAMES` (`:102-108`) | 21 LLM-engine names; no media engines | Replace the hand list with a **data-derived denylist**: every service id, model id, upstream model, workflow id and voice id found in `proxy_config.yml` seeds, `manifests/`, `presets/`, `providers/catalog/`, plus a short static list of engine names. Fail on any such string literal **anywhere in executable code** (not only in `if` conditions): arguments, defaults, dict keys and `==` on any variable name. Served-protocol vocabulary is exempt by construction, because it lives only in `gateway/protocols/*` and route tables, which the guard identifies by module, not by an allowlist. |

## 7. Walk-through: can an operator add a new engine/model/provider without code?

Full trace: `~/.tmp/eka/walkthrough.md` (subagent, read-only; UI statements unverified in a browser).

| Case | Result | Code needed (file:line, why) |
|---|---|---|
| **1. New OpenAI-compatible TTS server "FooTTS"** with voices `aria`/`bram`, model `foo-tts`, in the `tts-1` tier | **Works without code**, but not from the UI. Steps: `POST /admin/api/operator/services` (caps `[audio, speech]`, priority ≥ 200); add `model_aliases.speech.engines.foo-tts` (`voices`, `default_voice`, `voice_map`) and `stable.tts` entry in `proxy_config.yml`; `scripts/safe-restart`. `/v1/audio/voices` and Try's picker then list the voices from data | Soft gaps: UI presets are LLM-only (`service_registry.py:904-966`), so a dialog-made TTS service becomes a chat backend; `model_aliases` is file-only and needs a restart (`settings_api.py:348` `aliases_writable: False`; hot reload re-uses the boot snapshot `proxy.py:~370`); the `"tts"` service is a hard gate (E-26); http-only URL without `base_path` (E-27); no speech transforms (E-28); `engines` map needs `voice_map` (E-29); engine id absent from `/v1/models` |
| **2a. OpenAI-compatible image server** | **Hard blocker** | `/v1/images/*` always calls `proxy_comfy_request` (`proxy.py:8036`, `22640`); about 85 `"comfyui"` literals; capabilities only from `backends["comfyui"]` (`local_capabilities.py:379-480`); image aliases cannot have engine tiers (`stable_aliases.py:55`). Fix: E-40, E-43, E-44, E-45 |
| **2b. New non-ComfyUI workflow runtime** | **Hard blocker** | Same as 2a, plus extension-package `adapters`/`translators` are metadata only (`extension_packages.py` `CODE_OBJECT_KINDS`), and manifests know only `comfyui` and `gateway` bindings. Fix: E-42 |
| 2c. New ComfyUI **model** | Works without code: workflow JSON + manifest + `model_aliases.images` entry + restart | — |
| **3. New hosted OpenAI-compatible LLM provider** | **Works without code**: `POST /admin/api/providers` with the `generic-openai` preset and your own `provider_id`/`base_url`, or a new `providers/catalog/<id>.yml` (re-read per call) | Soft gaps: UI adds one provider per catalog preset (`web/src/app/providers/page.tsx:1787,1834`); remote providers are chat-only (`provider_catalog.py:20`); provider model records drop `model_metadata`/`context_window` (`provider_configuration.py:109-134`); prices mandatory (`:154`) |
| **4. New local swap LLM engine, new id** | Registers and routes from data (driver, `driver_config`, `models[]`, `context_window`). **Defect: re-swaps on every model-targeted request** | `_currently_loaded_model_for` (`proxy.py:1327-1339` ✔) returns `None` for any id outside the quarantine tables, so the already-loaded short-circuit never matches: systemd restarts the unit and http-load-unload reloads every time. Also: no aux probe (`:1342-1350` ✔), vision-via-aux kobold-only (`:3372-3384`), background removal requires an active kobold/tabby (`:6721`), `"tabby-config"` params provider is code (`lifecycle_drivers.py:836-850` ✔). Workarounds: `pin_loaded_model: true`, or re-use the record id `kobold`/`tabby`. The guard forbids adding the new id to the quarantine. Fix: section 4 |

## 8. Migration order (PR-sized slices, each shippable alone)

Each slice must:

- keep the full suite green;
- prove byte-identical outbound requests and responses for the current config (extend `tests/engine_p2_differential.py` with a sanitized `kobold`/`comfyui` registry fixture);
- include a replacement test: rename the service id, or add a second engine with only data, and require the same behavior;
- delete its guard entries in the same PR.

| # | Slice | Contents | Removes | Size |
|---|---|---|---|---|
| 1 | **Guard first (ratchet, not allowlist)** | Expand scan to all production Python; data-derived denylist; scan all literals, not only branch conditions. Record today's findings as a **shrinking count per file** that each later slice must lower and that may never grow. Delete the empty class/flag allowlist dicts | blind spots | S |
| 2 | Ollama envelope via frontend object | `resource_admission_http.py:147,153` → `protocols/ollama.py` method | `_ENGINE_BRANCH_ALLOWLIST` (now empty → deleted) | S |
| 3 | Default-model literals | E-22, E-23, E-24, E-25, E-49, E-50, E-62; missing config gives an honest error; seed the current values in `proxy_config.yml` | 12 literals | S |
| 4 | Speech engine quirks to rows | E-20, E-21 (`custom_voice.reference_transcript`, `placeholder_voice` on the omnivoice row); E-29 | `engine_id == "omnivoice"` | S |
| 5 | Activation and default-model fields | `activation.model_source`, `activation.require_absolute_path`, `default_model` on tabby; consumers `:3162,3171,3230,3232` | `_HAS_DEFAULT_MODEL_FALLBACK`, `_ACTIVATION_FROM_UPSTREAM_ID`, `_ACTIVATION_REQUIRES_ABS_PATH` | S |
| 6 | Loaded-state source | `driver_config.loaded_state` (`activation_record` + `http`); seed kobold/tabby; rewrite `:1335-1349, 3527-3533, 13545-13575`; delete the named probes, `_kobold_state`, `_BACKEND_STATE_STRATEGIES`, `state_type`, the pgrep probe. Test: a new id `llamasrv` with the same rows does **not** re-swap | `_LOADED_MODEL_IS_PATH`, `_LOADED_MODEL_IS_HTTP_ID`, `_HAS_AUX_MMPROJ` + 4.2 table | M |
| 7 | Aux vision and roles | `aux_grants_input_modalities`, `roles_resolve_to_loaded_model`; rename `activation_mmproj`→`activation_aux` with read-fallback (E-9) | `_VISION_VIA_AUX`, `_ROLES_MAY_BE_UNPINNED` | S-M |
| 8 | Context-window source | `context_window_source` field; delete `_kobold_context_window`, Tabby params path; replace `"tabby-config"` provider with `load_body` data | both `_CONTEXT_WINDOW_*` tables, `PARAMS_PROVIDERS` | M |
| 9 | Local-swap set and host globals | `supports_swap` for `:723, 24233, 24244`; `:6721` via chat routing; E-1, E-2, E-3, E-4, E-5, E-6, E-10, E-12, E-14 | `LOCAL_SWAP_BACKENDS`, **`engine_quarantine.py` deleted**; guard asserts it is absent | M |
| 10 | HttpLoadUnloadDriver without Tabby defaults | E-13 (required fields, honor `activation_model_key`) | Tabby-shaped defaults | M |
| 11 | Protocol code stops branching on engines | E-80, E-81, E-82 (Responses translator, reasoning wire); E-84, E-85 (`toolcall_markers`); E-83 (OpenAI name rows to `compat.aliases`) | Qwen/vLLM knowledge in protocol code; P2-2 anchor | M |
| 12 | Presets as data | `PROVIDER_PRESETS`, `_CAPABILITY_CHOICES`, ComfyUI field schema to `presets/*.yml`; add speech/STT/image presets so the UI can create them | code presets; walk-through S1.1 | S |
| 13 | Speech gate and transport | E-26, E-27, E-28; optional admin API write for `model_aliases` | `"tts"` gate; walk-through S1.3-S1.5 | M |
| 14 | Generation routes resolve services by binding | E-40, E-41, E-44, E-48, E-89; manifests use `bindings.<service_id>` (read-fallback for `comfyui`) | about 85 `"comfyui"` literals | M |
| 15 | OpenAI-images passthrough + image tiers | E-43, E-45: an image service with `protocol: openai-images` is forwarded; images/videos get `engines` tiers | **walk-through 2a blocker** | M |
| 16 | Generic graph-job runner | E-42: `config.job_api` path/field templates; ComfyUI values become a preset row; websocket progress optional | ComfyUI wire client; **2b blocker** | L (split: submit/status/fetch → upload/cancel/free → progress ws) |
| 17 | Workflow artifacts instead of graph code | E-46 (UI twin and schema as data), E-51, E-52, E-53, E-54, E-55, E-57, E-58, E-60, E-61, E-63 | ComfyUI graph knowledge | L (several PRs) |
| 18 | Chain recipes as templates | E-56 (LTX 2.5 and H3 builders → template + generic chain planner) | `video_extend*.py` engine code | L |
| 19 | Status/firehose/metrics from service rows | E-11, E-30, E-47, E-64, E-70-E-73, E-90; dual-write metrics, then rename | status-side engine code | M-L (2-3 PRs) |
| 20 | Remaining providers/identity | E-15, E-59, E-86, E-87, E-88, E-16 | last items | M |

Guard end state after slice 20:

- no quarantine module, no allowlist dicts, no per-file ratchet counts left;
- the denylist is derived from data;
- the only names in code are the served protocol modules and the LiteLLM dependency.

Order rationale:

- Slices 1-2 stop new debt immediately.
- Slices 3-9 empty the quarantine using small, independent fields. Slice 6 fixes the real defect: re-swap for a new engine id.
- Slices 14-16 remove the only hard blocker.
- Slices 17-19 are large but isolated.

## 9. Open points and confidence

- **Owner decision needed (cost, not principle):** delete Tabby support outright (disabled in the live registry) versus migrate its six properties as data. Deleting it is cheaper. Either choice satisfies "zero quarantine".
- Line numbers outside the ✔ rows come from subagent and dot reads at `a71e3e5c`; expect ±5 drift in groups. Class counts are ±10%.
- Not verified: browser behavior of the admin dialog and Try page; whether `KOBOLD_MODEL_DROPIN`/`GGUF_DIR`/`TABBY_SERVICE` are dead; `llm_execution.py` field allowlist intent (E-15); Tabby `load_body` and `default_model` values (seed rows must be read from the Tabby config at migration time).
- A grep-and-read audit cannot prove absence: engine knowledge with no recognisable name (e.g. numeric constants tuned for one model, such as `identity_scene.py:282-288` resolution tiers) may remain. The data-derived guard in slice 1 narrows this but does not close it.

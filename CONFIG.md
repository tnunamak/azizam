# Configuration reference

All configuration is optional unless stated otherwise. The gateway runs
without any of these env vars set; features that depend on a missing path are
silently disabled (they return empty data rather than crashing).

## Core gateway

| Env var | What it controls | Default |
|---|---|---|
| `AZIZAM_PORT` | Listener port | `5000` |
| `AZIZAM_SETUP_ALLOWED_CIDRS` | Exact client networks allowed to claim the first password owner; overrides `auth.setup_allowed_cidrs`. Loopback is always allowed. This does not enable anonymous inference access. | `auth.lan_bypass_cidrs` when neither setup setting exists |
| `AZIZAM_DOCKER_SUBNET` | Compose bridge subnet; configure with its gateway to avoid local subnet conflicts. | `172.30.251.0/24` |
| `AZIZAM_DOCKER_GATEWAY` | Compose bridge host address; Compose grants first-owner setup to this exact `/32` peer. Its published port remains loopback-only. | `172.30.251.1` |
| `AGW_REPO_PATH` | Absolute path to the checkout; used by the status endpoint for git-describe and drift checks | Detected from `__file__` at startup |

## Engine integrations

| Env var | What it controls | Default |
|---|---|---|
| `AGW_TABBYAPI_DIR` | Root directory of a [TabbyAPI](https://github.com/theroyallab/tabbyAPI) installation. When unset, TabbyAPI token auto-load, config read, and subprocess management are all disabled. | unset (feature off) |
| `AGW_COMFY_DIR` | Root directory of a [ComfyUI](https://github.com/comfyanonymous/ComfyUI) installation. Sets the default for `AGW_COMFY_OUTPUT_DIR` and `AGW_COMFY_CUSTOM_NODES_DIR`. | unset (feature off) |
| `AGW_COMFY_OUTPUT_DIR` | ComfyUI output directory (overrides `AGW_COMFY_DIR/output`). Used for recent-renders and disk hot-spot reporting. | `$AGW_COMFY_DIR/output` if `AGW_COMFY_DIR` is set, else disabled |
| `AGW_COMFY_CUSTOM_NODES_DIR` | ComfyUI custom_nodes directory (overrides `AGW_COMFY_DIR/custom_nodes`). Used by the status endpoint custom-nodes list. | `$AGW_COMFY_DIR/custom_nodes` if `AGW_COMFY_DIR` is set, else disabled |

## Status / monitoring

Service records can declare `health_endpoint` and `health_success_path` in
their configuration. The latter is a dot-separated JSON object path: an HTTP
2xx response whose selected value is neither null nor false is positive health
evidence. Explicit readiness failure, starting or busy states take precedence.
For example, a `/system_stats` probe can declare `health_success_path: system`.
An opaque JSON response without such a declaration remains unknown. These
fields are operator-authored registry data and apply after a registry edit;
changing bootstrap seeds does not overwrite an existing service record.

| Env var | What it controls | Default |
|---|---|---|
| `AGW_GPU_BUDGET_PATH` | Path to a shell script that exports GPU VRAM budget variables (e.g. `GPU0_TOTAL`, `GPU0_DESKTOP`). When unset, budget data is omitted from `/v1/status`. | unset (feature off) |
| `AGW_VRAM_ESTIMATES_PATH` | Path to the persistent VRAM-per-(workflow,size) JSON cache. | `<repo>/cache/vram_estimates.json` |
| `AGW_DRIFT_REPORT_PATH` | Path to the workflow drift status JSON written by `scripts/check_workflow_drift.py`. | `<repo>/cache/drift_status.json` |
| `AGW_IMAGE_CACHE_DIR` | Directory for the content-addressed image cache used by `response_format: "url"` results. | `<repo>/cache/images` |

## Daisy integration (optional, personal-agent specific)

These env vars connect the status endpoint to a [Daisy](https://github.com/daisy-agent/daisy) instance running locally. All are disabled when unset; the status endpoint still works normally without them.

| Env var | What it controls | Default |
|---|---|---|
| `AGW_DAISY_DIR` | Root directory of a Daisy installation. Sets the default for `AGW_DAISY_AUDIT_PATH` and `AGW_DAISY_MODELS_JSON`. | unset |
| `AGW_DAISY_AUDIT_PATH` | Path to Daisy's `audit.jsonl`. Used to surface recent Daisy decisions in `/v1/status`. | `$AGW_DAISY_DIR/audit.jsonl` if `AGW_DAISY_DIR` is set, else disabled |
| `AGW_DAISY_MODELS_JSON` | Path to Daisy's `models.json` (context-window budget file). | `$AGW_DAISY_DIR/.pi/agent/models.json` if `AGW_DAISY_DIR` is set, else disabled |

## LiteLLM sidecar (Docker Compose)

When using `docker compose up`, these are the relevant env vars (set in `.env`
or passed as environment variables):

| Env var | What it controls | Default |
|---|---|---|
| `AZIZAM_LITELLM_BASE_URL` | URL of the LiteLLM sidecar as seen by the gateway container | `http://litellm:4000` |
| `AZIZAM_LITELLM_API_KEY` | Shared secret between the gateway and the LiteLLM sidecar | `sk-local-litellm` |

## Notes

- Relative paths in `proxy_config.yml` (e.g. `cache_dir: cache/images`) are
  resolved relative to the proxy's working directory (the repo root when using
  `start.sh` or the Docker image).
- `proxy_config.yml` can reference environment variables for secrets using the
  `${env:VAR_NAME}` syntax. See
  [`docs/design/ENGINE_REGISTRY_DESIGN.md`](docs/design/ENGINE_REGISTRY_DESIGN.md)
  for the env-ref convention.
- `backends.<service>.chunking.global_concurrency` is a legacy-named,
  process-local limit. The gateway has no durable multi-worker STT coordinator,
  so each additional Python worker multiplies allowed uploads and reserved disk
  budget.

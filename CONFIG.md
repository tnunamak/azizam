# Configuration reference

All configuration is optional unless stated otherwise. The gateway runs
without any of these env vars set; features that depend on a missing path are
silently disabled (they return empty data rather than crashing).

## Core gateway

The checked-in `proxy_config.yml` sets `proxy.host` to `0.0.0.0`.
It sets `auth.setup_allowed_cidrs` (the networks that may claim the first
owner) to loopback, `192.168.0.0/16`, `10.0.0.0/8` and `100.64.0.0/10`.
It sets `auth.trusted_proxies` to `192.168.1.4`.
Protected API requests need an App key unless an owner has added the client
address to an App under **Apps & keys → app menu → Trusted addresses**.
Without a key or matching trusted address, the response is `401 key_required`.
A sent key always wins; a placeholder such as `loopback` returns
`401 key_invalid`, even from a trusted address. `X-Forwarded-For` counts only
when the request comes through `auth.trusted_proxies`. Browser sign-in uses
sessions. Authentication cannot be disabled, and there is no global LAN
bypass or unauthenticated Ollama discovery setting.
The gateway refuses to start while `auth.enabled`,
`auth.lan_bypass_enabled`, `auth.lan_bypass_cidrs`, `auth.path_tokens`,
`auth.ollama_compat_discovery` or the env vars `AZIZAM_AUTH_ENABLED`,
`AZIZAM_LAN_BYPASS` and `OLLAMA_COMPAT_DISCOVERY` are set.
These are deployment-specific values, not an all-features-off baseline.
Environment variables and Compose port publishing can override them. Check
the effective listener, published ports, trusted proxies and setup networks
before exposing the gateway.

| Env var | What it controls | Default |
|---|---|---|
| `AZIZAM_PORT` | Listener port | `5000` |
| `AZIZAM_SETUP_ALLOWED_CIDRS` | Exact client networks allowed to claim the first password owner; overrides `auth.setup_allowed_cidrs`. Loopback is always allowed. This does not enable anonymous inference access. | loopback only when neither setup setting exists |
| `AZIZAM_DOCKER_SUBNET` | Compose bridge subnet; configure with its gateway to avoid local subnet conflicts. | `172.30.251.0/24` |
| `AZIZAM_DOCKER_GATEWAY` | Compose bridge host address; Compose grants first-owner setup to this exact `/32` peer. Its published port remains loopback-only. | `172.30.251.1` |
| `AZIZAM_REPO_PATH` | Absolute path to the checkout; used by the status endpoint for git-describe and drift checks | Detected from `__file__` at startup |

## Identity provider sign-in

The owner can set **Provider display name** in **Settings → Sign-in**.
It is public text shown as “Continue with {name}”. The `admin.oidc.display_name`
bootstrap setting and the environment variable below use the same validation:
at most 80 characters, no angle brackets, control characters, or line separators.
Leading and trailing spaces are removed. An empty name shows “Continue with
single sign-on”. A saved Settings value overrides the bootstrap name; saving
an empty name clears it even when the environment variable is set.

| Env var | What it controls | Default |
|---|---|---|
| `AZIZAM_ADMIN_OIDC_DISPLAY_NAME` | Public provider name; overrides `admin.oidc.display_name` at bootstrap | empty (single sign-on) |
| `AZIZAM_OIDC_CLIENT_SECRET` | Client secret when referenced by `admin.oidc.client_secret_env` | unset |

## Engine integrations

| Env var | What it controls | Default |
|---|---|---|
| `AZIZAM_COMFY_DIR` | Root directory of a [ComfyUI](https://github.com/comfyanonymous/ComfyUI) installation. Sets the default for `AZIZAM_COMFY_OUTPUT_DIR` and `AZIZAM_COMFY_CUSTOM_NODES_DIR`. | unset (feature off) |
| `AZIZAM_COMFY_OUTPUT_DIR` | ComfyUI output directory (overrides `AZIZAM_COMFY_DIR/output`). Used for recent-renders and disk hot-spot reporting. | `$AZIZAM_COMFY_DIR/output` if `AZIZAM_COMFY_DIR` is set, else disabled |
| `AZIZAM_COMFY_CUSTOM_NODES_DIR` | ComfyUI custom_nodes directory (overrides `AZIZAM_COMFY_DIR/custom_nodes`). Used by the status endpoint custom-nodes list. | `$AZIZAM_COMFY_DIR/custom_nodes` if `AZIZAM_COMFY_DIR` is set, else disabled |

## Status / monitoring

Service records can declare `health_endpoint` and `health_success_path` in
their configuration. The latter is a dot-separated JSON object path: an HTTP
2xx response whose selected value is neither null nor false is positive health
evidence. Explicit readiness failure, starting or busy states take precedence.
For example, a `/system_stats` probe can declare `health_success_path: system`.
An opaque JSON response without such a declaration remains unknown. These
fields are operator-authored registry data and apply after a registry edit;
changing bootstrap seeds does not overwrite an existing service record.

GPU memory and RSS are attributed to services from the same records. A service
owns the processes in its `lifecycle.driver_config.unit` cgroup; a service with
no unit and a local `host`/`port` owns the process listening on that port.
Other processes are reported as `desktop`, and `__unattributed` is device-used
memory minus the sum of the per-process rows. A service can declare
`vram_budget_key` (a variable in the `AZIZAM_GPU_BUDGET_PATH` script, e.g.
`GPU1_LLM`) to be compared with that budget on the status page, using the
memory on the GPU where its process holds the most; a share on another GPU is
shown as spillover with no budget verdict. The sampler
writes the same rows to `metrics.db` as `service_vram_mb` (tags `gpu`,
`service`; slow tier, once a minute). Each 10 s tick it also writes
`vram_free_mb` and `gpu_desktop_alloc_failures` (count of kernel "Failed to
allocate NVKMS memory" lines logged since the last read). The sampler reads the
kernel journal on from a cursor that it commits to `metrics.db` (table
`sampler_state`) in the same transaction as the counts, so failures logged while
the journal was unreadable, a write failed or the gateway was down count once,
on the next tick that reads them. A tick reads at most 2000 journal entries, so
a large backlog is caught up over several ticks. While the journal is unreadable
there is no row and the gateway logs one warning. With no committed cursor
(first run) counting starts at the journal's end. If the committed cursor is no
longer in the journal (rotated away), the gateway logs the gap and restarts at
the journal's end: failures in that gap are not counted, and none are counted
twice.

| Env var | What it controls | Default |
|---|---|---|
| `AZIZAM_GPU_BUDGET_PATH` | Path to a shell script that exports GPU VRAM budget variables (e.g. `GPU0_TOTAL`, `GPU0_DESKTOP`). When unset, budget data is omitted from `/v1/status`. | unset (feature off) |
| `AZIZAM_VRAM_ESTIMATES_PATH` | Path to the persistent VRAM-per-(workflow,size) JSON cache. | `<repo>/cache/vram_estimates.json` |
| `AZIZAM_DRIFT_REPORT_PATH` | Path to the workflow drift status JSON written by `scripts/check_workflow_drift.py`. | `<repo>/cache/drift_status.json` |
| `AZIZAM_IMAGE_CACHE_DIR` | Redirects only the image-storage figure in monitoring. Generated images use `images.cache_dir`. | `<repo>/cache/images` |

## Media URL inputs

Media inputs accept multipart bytes, asset IDs owned by the calling API key,
data URIs, base64, and HTTP(S) URLs. Filesystem paths are rejected. Gateway
content URLs on the public hosts or configured instance base URL resolve
internally, with signature and ownership checks.
The current request's `Host` and `X-Forwarded-Host` are also recognized so
signed URLs minted on local or unconfigured gateway hostnames can be reused.
Recognizing a host permits only internal content resolution, never a network
fetch to that host.

| Env var | What it controls | Default |
|---|---|---|
| `AZIZAM_MEDIA_FETCH_MAX_BYTES` | Maximum bytes in one URL download. Responses stream under this cap; each redirect is checked. | `268435456` (256 MiB) |
| `AZIZAM_MEDIA_FETCH_ALLOW_PRIVATE_HOSTS` | Comma-separated exact hostnames or IPs that may resolve to RFC 1918 or IPv6 ULA addresses. No wildcards. This does not permit loopback, link-local, or metadata endpoints. | empty |

URL downloads use a 10-second DNS/connect timeout, 30-second read timeout,
60-second total timeout, and at most three redirects. Connections use a checked
IP, the original HTTP Host and TLS hostname, and no environment proxies.
If a checked address cannot connect, the fetch tries the other checked
addresses without resolving the hostname again. IPv6 transition addresses
are checked against their embedded IPv4 destination; IPv6 site-local
addresses are blocked.
Servers must honor `Accept-Encoding: identity` and send uncompressed media.

## Daisy integration (optional, personal-agent specific)

These env vars connect the status endpoint to a [Daisy](https://github.com/daisy-agent/daisy) instance running locally. All are disabled when unset; the status endpoint still works normally without them.

| Env var | What it controls | Default |
|---|---|---|
| `AZIZAM_DAISY_DIR` | Root directory of a Daisy installation. Sets the default for `AZIZAM_DAISY_MODELS_JSON` only. Set `AZIZAM_DAISY_AUDIT_PATH` separately. | unset |
| `AZIZAM_DAISY_AUDIT_PATH` | Path to Daisy's `audit.jsonl`. Set it separately to surface recent Daisy decisions in `/v1/status`; `AZIZAM_DAISY_DIR` supplies no audit-path default. | unset (disabled) |
| `AZIZAM_DAISY_MODELS_JSON` | Path to Daisy's `models.json` (context-window budget file). | `$AZIZAM_DAISY_DIR/.pi/agent/models.json` if `AZIZAM_DAISY_DIR` is set, else disabled |

## LiteLLM sidecar

The chat sidecar runs in one of two launch modes, and the defaults differ:

- **Docker Compose:** LiteLLM runs in its own container. The gateway reaches it
  at `http://litellm:4000`.
- **`start.sh` (bare metal):** the launcher starts LiteLLM on `127.0.0.1:4011`
  and generates any key that you do not set.

| Env var | What it controls | Default |
|---|---|---|
| `AZIZAM_LITELLM_BASE_URL` | URL of the LiteLLM sidecar as seen by the gateway | Compose: `http://litellm:4000`. `start.sh`: `http://127.0.0.1:4011` |
| `AZIZAM_LITELLM_API_KEY` | Shared secret between the gateway and the LiteLLM sidecar. Treat it as a secret. | Compose: `sk-local-litellm`. `start.sh`: generated when unset |
| `LITELLM_MASTER_KEY` | Master key for the sidecar | `start.sh`: the value of `AZIZAM_LITELLM_API_KEY`. Compose: the same shared key |
| `AZIZAM_INTERNAL_LITELLM_KEY` | Internal key used by the gateway for the sidecar. Treat it as a secret. | `start.sh`: generated when unset |
| `AZIZAM_MANAGE_LITELLM` | Set to `false` to stop `start.sh` from starting and watching its own LiteLLM process | `true` |
| `AZIZAM_LITELLM_CONFIG_PATH` | Generated sidecar config file (`start.sh`) | `data/litellm.config.yaml` |
| `AZIZAM_LITELLM_PID_FILE` | PID file for the sidecar that `start.sh` manages | `data/litellm.pid` |
| `AZIZAM_LITELLM_RELOAD_INTERVAL` | Seconds between checks of the sidecar config (`start.sh`) | `2` |
| `AZIZAM_PRISMA_PREFLIGHT_TIMEOUT` | Seconds allowed for Prisma checks and client generation (`start.sh`) | `90` |
| `DATABASE_URL` | When set, `start.sh` checks Prisma first. It may remove an incomplete Node cache at `PRISMA_NODEENV_DIR`. | unset |
| `PRISMA_NODEENV_DIR` | Node cache that the Prisma check may repair (`start.sh`) | `$HOME/.cache/prisma-python/nodeenv` |

## Launcher and restart script

`start.sh` and `scripts/safe-restart` read these variables. Set them in the
shell, or in `.env.local` for `start.sh`. `start.sh` sources `.env.local` when
the file exists. The file is gitignored.

| Env var | What it controls | Default |
|---|---|---|
| `AZIZAM_CONFIG_PATH` | Bootstrap config file that the gateway loads. The first-run setup uses `first-run.yml`, which listens on loopback only. | `proxy_config.yml` |
| `AZIZAM_REQUIREMENTS_FILE` | Requirements file that `start.sh` installs | `requirements.txt` |
| `AZIZAM_REQUIREMENTS_HASHES` | Set to `true` to install with `--require-hashes`. Any other value installs without hash checks. | `false` |
| `AZIZAM_ADMIN_KEY` | Admin key for `safe-restart`. A non-blank value wins over the key file. | unset |
| `AZIZAM_ADMIN_KEY_FILE` | Admin key file for `safe-restart`. Keep it private. | `~/.config/azizam/admin-key` |
| `AZIZAM_URL` | Loopback gateway URL that `safe-restart` controls | `http://127.0.0.1:5000` |
| `AZIZAM_PUBLIC_URL` | Public URL that `safe-restart` checks after a restart. Empty skips the check. | empty (check skipped) |

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

## Request body limits

Backend requests forward only `Content-Type`, `Accept`, and `User-Agent` from
clients, with `Accept-Encoding: identity`. Configure any backend-specific
headers and authentication through downstream `headers` or backend
`auth_inject` rules. Gateway cookies, CSRF tokens, internal headers and
credentials in query parameters are not forwarded to inference backends.
Native provider paths containing dot segments, encoded delimiters, backslashes
or double slashes return HTTP 400 before policy evaluation or forwarding.

The gateway checks `Content-Length` before routing and counts incoming body
chunks before passing them to a parser. Requests over the selected limit return
HTTP 413 with the OpenAI error code `request_too_large`. Chat authentication runs
before JSON body reads. A declared oversized body may receive 413 before auth.

| Env var | Config field | Default |
|---|---|---|
| `AZIZAM_REQUEST_BODY_JSON_BYTES` | `request_body_limits.json_bytes` | 33,554,432 bytes (32 MiB) |
| `AZIZAM_REQUEST_BODY_MEDIA_BYTES` | `request_body_limits.media_bytes` | 537,919,488 bytes (513 MiB) |

Environment values override configuration. Both must be positive byte counts;
changes apply when the gateway starts. Image, audio, video, gateway media,
workflow and identity routes, and `/sdapi/v1` use the media limit. `/openai/v1`
media aliases use the same limit. All other routes use the JSON limit, regardless
of the supplied `Content-Type`. The media default leaves 1 MiB of multipart
overhead above STT's existing 512 MiB file limit. Route-specific file and field
limits still apply.

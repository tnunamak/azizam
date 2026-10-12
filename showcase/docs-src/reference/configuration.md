---
title: Configuration
summary: Settings for the listener, setup, request limits, media downloads and monitoring.
---

# Configuration

The gateway reads its settings from a configuration file and from environment variables. An environment variable overrides the matching field in the file. Most settings take effect when the gateway starts.

Relative paths in the configuration file resolve from the gateway's working directory. In the Docker image, that is the root of the repository.

Do not write secret values into the configuration file. Settings that need a secret take the name of an environment variable instead, for example `api_key_env: NAME`.

## Listener and network

| Variable | Default | Effect |
| --- | --- | --- |
| `AZIZAM_PORT` | `5000` | The port the gateway listens on. In Docker Compose, the host port. |
| `AZIZAM_DOCKER_SUBNET` | `172.30.251.0/24` | The Compose network's subnet. |
| `AZIZAM_DOCKER_GATEWAY` | `172.30.251.1` | The Compose network's gateway address. Change it together with the subnet. |
| `AZIZAM_SETUP_ALLOWED_CIDRS` | none; Docker Compose sets it to the Compose gateway address | Networks that may create the owner account. Overrides `auth.setup_allowed_cidrs`. Loopback is always allowed. |

## Request limits

| Variable | Configuration field | Default |
| --- | --- | --- |
| `AZIZAM_REQUEST_BODY_JSON_BYTES` | `request_body_limits.json_bytes` | 33,554,432 bytes (32 MiB) |
| `AZIZAM_REQUEST_BODY_MEDIA_BYTES` | `request_body_limits.media_bytes` | 537,919,488 bytes (513 MiB) |

Values must be positive byte counts. Changes take effect when the gateway starts.

The media limit applies to image, audio, video, face, media job, workflow and `/sdapi/v1` routes, and to the media routes under `/openai/v1`. All other routes use the JSON limit, whatever their `Content-Type`. A single file or field can have a smaller limit. A request over the limit gets HTTP 413 with the code `request_too_large`.

## Media downloads

When a request gives a media URL, the gateway downloads it with these limits:

| Setting | Value |
| --- | --- |
| `AZIZAM_MEDIA_FETCH_MAX_BYTES` | Largest download, in bytes. Default 268,435,456 (256 MiB). |
| `AZIZAM_MEDIA_FETCH_ALLOW_PRIVATE_HOSTS` | Comma-separated exact host names or IP addresses that may resolve to private network addresses. No wildcards. Empty by default. |
| Redirects | At most three. Each one is checked again. |
| Timeouts | 10 seconds to resolve and connect, 30 seconds per read, 60 seconds in total. |
| Encoding | The server must send the media uncompressed. |

The allow list cannot open loopback, link-local or cloud metadata addresses.

## Monitoring

| Variable | Default | Effect |
| --- | --- | --- |
| `AZIZAM_GPU_BUDGET_PATH` | none | Shell script that sets GPU memory budget variables. Without it, status shows no budgets. |
| `AZIZAM_VRAM_ESTIMATES_PATH` | `<repo>/cache/vram_estimates.json` | Cache of memory estimates for workflows. |
| `AZIZAM_DRIFT_REPORT_PATH` | `<repo>/cache/drift_status.json` | Workflow drift report. |
| `AZIZAM_IMAGE_CACHE_DIR` | `<repo>/cache/images` | Folder measured for the image storage figure in status. |

`AZIZAM_IMAGE_CACHE_DIR` only changes what status measures. To change where creations are stored, set `images.cache_dir` in the configuration file. The default is `cache/images` under the gateway's working directory.

An engine record can name a budget variable with `vram_budget_key`. Status compares the engine's memory use on the GPU where it uses the most memory with that budget. Memory on other GPUs shows as spillover.

## Transcription concurrency

`backends.<engine>.chunking.global_concurrency` applies per gateway process, despite its name. If you run more than one gateway process, each one allows that many uploads and reserves its own disk budget.

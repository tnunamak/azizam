# OpenAI Proxy Auth Guide

This proxy uses client-facing API keys that are separate from downstream service
keys. Agents and tools should authenticate to the proxy with their own `opk_...`
key. The proxy then maps that key to the backend credentials needed for Tabby,
Kobold, TTS, STT, or ComfyUI.

## Quick Start For Agents

Run this from the proxy directory:

```bash
cd /home/tnunamak/applications/openai-proxy
./venv/bin/python proxy.py generate-key <agent-name>
```

Example:

```bash
./venv/bin/python proxy.py generate-key simon
```

The command prints one client API key:

```text
opk_...
```

Use that value as the OpenAI API key for this proxy. Store it in the agent's
secret store or environment. Do not put the printed key in source files, logs,
or prompts.

Typical client settings:

```text
Base URL: http://127.0.0.1:5000/v1
API key:  opk_...
```

External clients can use the public proxy host when available:

```text
Base URL: https://openai.vivid.fish/v1
API key:  opk_...
```

## What The Command Does

`generate-key` creates or rotates an entry in `proxy_api_keys.yml`.

The file stores only a SHA-256 hash of the client key:

```yaml
keys:
  - name: simon
    key_hash: sha256:...
    downstream:
      tabby: {}
```

The raw `opk_...` key is shown only once when generated. If it is lost, generate
a new key for the same name and update that agent's client configuration.

If the same `<agent-name>` already exists, the command rotates that agent's
client key while preserving its downstream mapping.

## Request Auth Rules

All proxied traffic requires a valid proxy API key. `/health` is the only public
endpoint by default.

Accepted client auth headers:

```http
Authorization: Bearer opk_...
```

or:

```http
x-api-key: opk_...
```

or:

```http
api-key: opk_...
```

Do not send Tabby admin keys or downstream provider keys as client auth. The
proxy strips client auth headers before forwarding requests and injects the
appropriate downstream credentials itself.

## Smoke Test

After generating a key, test that the proxy accepts it:

```bash
export OPENAI_PROXY_API_KEY='opk_...'

curl -s http://127.0.0.1:5000/health

curl -s http://127.0.0.1:5000/v1/voices \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Expected behavior:

- `/health` returns `status: healthy` without auth.
- `/v1/voices` returns voices with a valid key.
- `/v1/voices` returns `401` without a key.

The full local test script also expects the key:

```bash
OPENAI_PROXY_API_KEY='opk_...' ./test_e2e.sh
```

## Downstream Credential Mapping

By default, proxy API keys map to the global downstream credentials configured
for the proxy:

- Tabby API key: `TABBY_API_KEY` or Tabby's `api_tokens.yml`
- Tabby admin key: `TABBY_ADMIN_KEY` or Tabby's `api_tokens.yml`
- TTS, STT, ComfyUI, Kobold: no extra auth unless configured per key

Per-agent overrides can be added in `proxy_api_keys.yml` when an agent needs
different downstream credentials:

```yaml
keys:
  - name: simon
    key_hash: sha256:...
    downstream:
      tabby:
        api_key_env: SIMON_TABBY_API_KEY
        admin_key_env: SIMON_TABBY_ADMIN_KEY
      tts:
        headers:
          Authorization:
            env: SIMON_TTS_API_KEY
            prefix: Bearer
      comfyui:
        headers:
          X-API-Key:
            env: SIMON_COMFY_API_KEY
```

Supported downstream keys:

- `tabby.api_key` or `tabby.api_key_env`
- `tabby.admin_key` or `tabby.admin_key_env`
- `<service>.headers` for arbitrary backend headers

Header values can be literal strings:

```yaml
headers:
  X-API-Key: local-dev-secret
```

or environment-backed values:

```yaml
headers:
  Authorization:
    env: SIMON_TTS_API_KEY
    prefix: Bearer
```

The proxy reloads `proxy_api_keys.yml` when it changes, so adding, rotating, or
deleting a key does not require a service restart.

## Rotation And Revocation

Rotate one agent's key:

```bash
./venv/bin/python proxy.py generate-key simon
```

Then update that agent to use the newly printed `opk_...` value.

Revoke an agent's key:

1. Remove that agent's entry from `proxy_api_keys.yml`.
2. Save the file.
3. The proxy will reject that key on subsequent `/v1` requests.

## Common Failures

`401 Missing API key.`

The request reached a proxied endpoint without `Authorization: Bearer opk_...`
or another accepted client auth header.

`401 Invalid API key.`

The client key does not match any hash in `proxy_api_keys.yml`.

`503 Proxy auth is enabled, but no proxy API keys are configured.`

Auth is fail-closed and no usable keys are loaded. Generate at least one key.

`502` or `503` backend errors.

The client key was accepted, but the selected downstream service was unavailable
or rejected its own backend credentials.

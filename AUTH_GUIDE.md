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
client key while preserving its downstream mapping and (when wired) its
`profile:` block.

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
deleting a key does not require a service restart. App profile blocks (see
below) reload alongside the key entries they live on.

## App Profiles

App profiles are the authorization and audience boundary for a key. Each key
entry may carry an optional `profile:` block that the gateway loads into an
`AppProfile` via `gateway.profiles.load_profile_from_entry`. When mounted in
the request path, the profile decides:

- which protocols the key may use (e.g. `openai.chat`, `openai.images`,
  `openai.audio`, `openai.responses`, `anthropic.messages`, `ollama.chat`,
  `ollama.generate`)
- which workflows, models, and backends the key may target
- whether cloud fallback is allowed
- whether — and where — native passthrough is permitted
- how much status, discovery, and receipt detail the key sees

Missing `profile:` blocks stay backward-compatible: the loader falls back to
`DEFAULT_PROFILE` (local-only, basic visibility), so existing keys keep
working unchanged.

Allow-lists are conservative by construction:

- omit a list → "no opinion" for that dimension (no restriction)
- set a list to `[]` → "deny all" for that dimension

`status_visibility.level` maps a key to a discovery/receipt audience tier:

| Level      | Audience       | What the key sees                              |
|------------|----------------|------------------------------------------------|
| `none`     | client         | No receipts; minimal discovery                 |
| `basic`    | client         | Client receipts (request/job IDs, status)      |
| `standard` | admin          | Routing facts, fallback reasons                |
| `full`     | internal       | Backend, policy source, manifest hash, diags   |

Example profile block on a key entry:

```yaml
keys:
  - name: image-agent
    key_hash: sha256:...
    profile:
      id: image-agent
      protocols: ["openai.images"]
      workflows: ["image.edit.controlnet.example"]
      models: ["sdxl-base"]
      backends: ["comfyui.local"]
      fallback:
        cloud_allowed: false
      status_visibility:
        level: basic
```

### Native passthrough

`native_passthrough` is opt-in and scoped per profile. It is not a blanket
ComfyUI DSL — it is a privileged lane for specific operations on specific
backends, and every use is recorded in receipts.

```yaml
profile:
  id: comfy-tinkerer
  native_passthrough:
    enabled: true
    allowed_backends: ["comfyui.local"]
    allowed_operations: ["queue_prompt", "history"]
    audit: true
```

### Gateway-owned controls live under extensions

Backend-native power (denoise, controlnet strength, model-specific knobs)
should not be added as global OpenAI fields. It belongs under namespaced
gateway extensions:

```json
{
  "extensions": {
    "com.vividfish.workflow": {
      "denoise": 0.35,
      "controlnet_strength": 1.1
    }
  }
}
```

Unknown extension inputs are rejected with a reason; known-but-unsupported
inputs are recorded as ignored. The proxy never silently drops inputs.

### Templates for current local keys

Safe starter profiles for the agent/UI/harness keys this proxy serves today.
Drop these under the matching key entry — they use non-secret placeholders
only. Tighten the allow-lists when a key has a narrow role; leave them
omitted to keep "no opinion" semantics until you need to restrict.

```yaml
# Agent/coding clients (Claude Code-style, Continue, Cline):
- name: simon
  key_hash: sha256:...
  profile:
    id: simon
    # Omit protocols/models to stay permissive; add lists when narrowing.
    fallback:
      cloud_allowed: false
    status_visibility:
      level: standard

# UI clients (LibreChat, Open WebUI):
- name: librechat
  key_hash: sha256:...
  profile:
    id: librechat
    protocols: ["openai.chat", "openai.responses", "openai.audio"]
    fallback:
      cloud_allowed: false
    status_visibility:
      level: basic

- name: open-webui
  key_hash: sha256:...
  profile:
    id: open-webui
    protocols: ["openai.chat", "openai.responses", "openai.audio", "openai.images"]
    fallback:
      cloud_allowed: false
    status_visibility:
      level: basic

# Compatibility harness / test client:
- name: claude-test
  key_hash: sha256:...
  profile:
    id: claude-test
    protocols: ["anthropic.messages"]
    fallback:
      cloud_allowed: false
    status_visibility:
      level: full

# Helper agents (daisy, cap): leave profile omitted to inherit
# DEFAULT_PROFILE (local-only, basic visibility) until a narrower
# boundary is needed.
```

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

# The Engine Registry — SLVP-Ideal Greenfield Design

**For:** Azizam
**Scope:** the service / engine / capability / lifecycle registry that replaces the ~74 hardcoded service-specific behaviors catalogued in `docs/design/SERVICE_HARDCODING_AUDIT.md`.
**Audience test:** an r/LocalLLaMA / r/selfhosted infra person reading the YAML should immediately understand it, trust it, and be able to reproduce their gateway from a Git checkout.

---

## What changed after red-team

Three adversarial reviewers (self-host skeptic, r/LocalLLaMA power user, DHH-style simplicity architect) tore into the draft. I verified every load-bearing claim against the actual code before accepting or rejecting it. Here is what moved:

**Cut (over-engineering the simplicity reviewer flagged, and one the power user flagged):**
- **`kind: plugin` transform escape hatch — DELETED.** It shipped "arbitrary Python here" with no sandbox/versioning/discovery design while claiming the four primitives cover everything. Cut from v1. If a real behavior exceeds the primitives we add a *named* primitive, not a generic plugin door.
- **`state: present | absent` — DELETED.** A flat `services.yml` already supports "delete the block to remove it"; `enabled: false` already disables. Three disable mechanisms is operator confusion. Authentik needs `absent` because blueprints are append-only chunks; our file isn't.
- **`kind: provider | llm-substrate` removed from operator-visible YAML.** Replaced with an internal `internal: true` flag the gateway sets on the LiteLLM substrate row only. Verified: code uses `kind in {"litellm","llm-substrate"}` purely as the substrate discriminator (`service_registry.py:471-476`), and the editability lock on that row is intentional and survives.
- **`source` 5-value enum collapsed to `origin: operator | discovered`.** Verified: `source` (`service_registry.py:35`) drives only provenance/import-merge, never routing. Free-text `created_via` carries audit detail.
- **`context-probe` reframed, not killed.** It was wrongly called a "transform" (transforms are per-request). It's now a `context_window: probe` setting on the health block. But I **kept probing as default-on for openai-compatible backends** against the simplicity reviewer's "drop it entirely" — because the power user proved pass-4 routing silently no-ops without it (most operators won't hand-declare context windows). Adjudication below.
- **`weight` / weighted-random — DELETED for v1.** Strict priority + round-robin at equal priority. Keeps the "Test route" trace deterministic, which is a top trust feature.

**Fixed (real-world breakage the power user and self-host reviewer found):**
- **`swap: exclusive` now has a real drain contract** (active-request counter, `drain_timeout_s`, `max_queue_wait_s`, explicit `swap_state`). The draft named the field but specified no mechanism — verified the current code (`kobold_swap_lock`/`tabby_swap_lock`, `proxy.py:789,1101`) serializes *callers* but never drains *in-flight streams*. This was a genuine lie in the draft.
- **systemd driver uses `subprocess systemctl`, not pystemd/D-Bus.** Verified: current code is `systemctl --user` subprocess (`proxy.py:746,1029-1030`); pystemd isn't a dependency. All three reviewers were right. `scope: user | system` is now an explicit modeled field with documented privilege prerequisites and a read-only preflight check surfaced in the UI.
- **`docker` lifecycle driver added.** It was completely absent; it's the most common r/selfhosted backend pattern. Now a first-class driver.
- **`qwen3-no-think` is a built-in transform, not a `param-map`.** Verified at `proxy.py:5019-5028`: it does messages-array surgery (append `/no_think` to an existing system message, else insert one). No declarative primitive expresses that. Added `kind: message-inject` primitive AND honestly relabeled this specific transform `kind: builtin`. The "four primitives cover all 74" claim is corrected to "four primitives + a small set of named built-ins."
- **`role` precisely defined as a health-alarm gate, never a routing gate.** Verified: code uses `role` only to suppress the down-alarm for standby services (`proxy.py:9730-9732`), never to pick a backend. Routing always tries the next healthy candidate regardless of role. Standby+unmanaged is now nailed down.
- **Wildcard (`*`) precedence specified:** explicit model match always beats wildcard; wildcard + unreachable backend = empty pool, fall through (never "try and let the backend error"). Added a `models.expose` policy (`mapped` default for local, `all` for cloud) so a local vLLM doesn't leak internal/LoRA model ids.
- **vLLM LoRA answered:** one adapter = one service record against the same host, `expose: mapped`; the `http-load-unload` driver covers `/v1/load_lora_adapter` pre-loading. It's no longer silent.
- **Ollama concurrency answered:** new `swap: engine-managed` value ("the engine schedules its own models; gateway gets out of the way") distinct from `exclusive` (single-slot llama-server) and `external`.

**Source-of-truth honesty fix (self-host + simplicity reviewers, both right):**
- The draft presented "file wins" as current architecture. It is **not** — verified `service_registry.py:10,280` say the DB *is* the runtime source of truth today. The design is now explicitly labeled a **target state** with a named three-state migration (file-as-seed → DB-authoritative [today] → file-wins [target]), and **the file-wins flip moves to migration Step 2**, not last, so later steps build on the correct authority model. To avoid the GitOps clobber race, write-through uses a **split file**: operator owns `services.yml` (git), the UI writes to `services.local.yml` (gitignored), with an explicit "Promote to registry" merge — Authentik/ArgoCD's actual pattern, not just its name.

**Kept against a critique (with reasoning):**
- **`context_window: probe` stays default-on** for openai-compatible backends, against the simplicity reviewer's "drop probing entirely." Reasoning: the power user proved that without it, pass-4 oversized-request routing is a silent no-op for the *majority* of records (operators won't hand-declare context). I addressed the staleness objection by tying the probe to the swap lifecycle (re-probe after any swap) and to the health cycle (re-probe on reconnect), and I made it observable. A static-only model is honest but ships a routing feature that never fires.
- **The `command` driver stays**, against the self-host reviewer's "cut it / it re-introduces process management." But I removed the `${PORT}` macro that made the objection valid — the operator declares a fixed `connection.port`; the gateway never allocates ports or owns supervision. With that cut, `command` is just "run this start/stop command the operator typed," which is squarely inside the boundary (drive a handle the operator declared). It's the honest answer for bare `llama-server` users who don't want to write a unit file.
- **The four-driver framing stays** (now five with docker), against "four is one too many." Each maps to a distinct, real operator deployment (none / systemd unit / HTTP load endpoint / docker container / typed command). Removing one forces an operator to adopt a tool they don't run.

---

## 0. Design axioms (the non-negotiables)

1. **Router first, orchestrator second.** Every record defaults to `lifecycle.driver: external`, `managed: false`. The gateway routes to an engine that already exists. A record with *only* connection + capability fields is complete and useful. Lifecycle is purely additive. *(Prior art: llama-swap `peers`/`managed:false`-by-omission; the audit's `managed:false` default for all current backends.)*

2. **The gateway drives handles, never existence.** A lifecycle driver issues `start`/`stop`/`swap`/`health` against an operator-declared handle (a systemd unit they wrote, an HTTP load endpoint the engine exposes, a docker container they run, a `cmd` they typed). It never writes a unit's `ExecStart`, never `docker run --pull`s an image it builds, never downloads weights, never `pip install`s an engine. Out-of-band setup (write the unit, expose the port, drop a token file, `docker compose up` once) is an *acceptable prerequisite*. *(Prior art: KServe `ServingRuntime`/`InferenceService` decoupling — the gateway never authors the runtime; jistr.com socket-activation — gateway owns the proxy unit, operator owns the engine unit.)*

3. **Capability is data, not a code branch.** No `if backend in ("kobold","tabby")`. No `EMBEDDINGS_HOST` constant. A request resolves to a backend by reading `capabilities` + `models` + `priority` off registry records. *(Prior art: LiteLLM `mode` as endpoint→backend discriminator; OpenRouter `input_modalities`/`output_modalities`.)*

4. **The committable file is the *target* source of truth; the DB is a reconciliation cache.** A committable YAML reproduces the running gateway from a checkout. **(Honest current state: today the DB is authoritative and YAML is import-seed only — see §5 and migration Step 2.)** The DB holds ephemeral facts (health history, last-used, live VRAM, swap locks). *(Prior art: Authentik blueprint reconciliation + Caddy "GET /config always exports canonical form"; the anti-pattern is the NPM "DB-only, no export" trap.)*

5. **Provider quirks are pluggable transforms, never hardcoded.** Each quirk (Qwen `/no_think`, voice maps, reasoning-effort translation, param dropping) is a named transform attached by id, resolved from a transform registry. *(Prior art: Portkey `override_params`/`default_params`/`drop_params`; LiteLLM `extra_body`/`additional_drop_params`.)*

---

## 1. THE DATA MODEL

### 1.1 The one record type: `service`

A service is one addressable engine process (or remote endpoint). **Only `id` and `connection` are required.** Everything else defaults so the router-only, externally-managed case is trivial.

```
service:
  # ── IDENTITY ──────────────────────────────────────────────
  id:            str          # stable key; what receipts/logs reference
  name:          str          # human label (default: id)
  enabled:       bool         # default true   (the one disable knob — no state:absent)
  origin:        enum         # operator | discovered   (provenance; never routes)
  created_via:   str          # free-text audit annotation (e.g. "ui-form","yaml-import")
  internal:      bool         # default false; true hides record from pools+admin
                              #   (gateway sets this ONLY on the LiteLLM substrate row;
                              #    never appears in operator-authored YAML)

  # ── CONNECTION ────────────────────────────────────────────  (required block)
  connection:
    host:         str         # 127.0.0.1
    port:         int
    base_path:    str         # default "/v1"
    scheme:       str         # default "http"
    health:
      endpoint:   str         # default "/v1/models" (openai-compatible), else "/health"
      method:     enum        # GET|HEAD (default GET)
      timeout_s:  int         # default 5  (per-service — kills global HEALTH_CHECK_TIMEOUT)
      context_window: enum    # declared | probe | off   (default: probe for
                              #   openai-compatible; see §4.2)

  # ── PROTOCOL / ADAPTER ────────────────────────────────────
  protocol:       enum        # openai-compatible | comfyui | custom
                              # default: openai-compatible
  native_passthrough: bool    # default true for openai-compatible
  adapter_config: {}          # protocol-adapter config (NOT lifecycle); typed per protocol

  # ── CAPABILITIES ──────────────────────────────────────────  (the routing discriminant)
  capabilities:   [str]       # closed vocab: chat, completions, embeddings,
                              #   image.generations, image.edits, audio.speech,
                              #   audio.transcriptions, audio.translations,
                              #   video.generations, rerank, vision (modifier)
  input_modalities:  [str]    # text,image,audio,video  (default ["text"])
  output_modalities: [str]    # text,image,audio,video  (default ["text"])
  context_window:    int|null # EFFECTIVE max tokens (operator-declared cap; null=unknown)
  supported_params:  [str]    # OpenRouter-style; enables implicit param filtering (optional)

  # ── MODEL ALIASES (public → upstream) ─────────────────────
  models:                     # omit → backend's own /v1/models is the catalog
    expose:      enum         # mapped | all   (default: mapped for local, all for cloud presets)
    list:
      - public:    str        # what the client sends (the "model group")
        upstream:  str        # what we forward as `model` (default: public)
        aliases:   [str]      # extra public names → this deployment
        context_window: int|null  # per-model override
        transforms: [str]     # per-deployment transform ids (§4)
        activation:           # per-model swap target; driver-typed (§3)
          env: {}             # {VAR: value} env lines the systemd/docker driver writes
          load_body: {}       # body for http-load-unload driver

  # ── PROVIDER TRANSFORMS ───────────────────────────────────  (pluggable, §4)
  transforms:     [str]       # service-wide transform ids, applied in order

  # ── PRIORITY / ROLE ───────────────────────────────────────
  priority:       int         # lower = tried first within a capability pool (default 100)
  role:           enum        # required | standby   (default required) — ALARM gate only (§2.3)

  # ── SECRETS (ref ONLY, never inline plaintext) ────────────
  secret_refs:
    api_key:     ${env:GROQ_API_KEY}
    auth_file:   ${file:/path/to/token}      # the "drop a token file" prerequisite

  # ── LIFECYCLE ─────────────────────────────────────────────  (additive; default = no-op)
  lifecycle:
    driver:      enum         # external | systemd | docker | http-load-unload | command
                              # default: external (managed:false, health-only)
    managed:     bool         # default false
    idle_timeout_s: int       # 0=never unload, -1=pin, >0=idle seconds (Ollama grammar)
    swap:        enum         # none | exclusive | engine-managed   (§3.4)
    drain_timeout_s: int      # default 30  — wait for in-flight requests before swap/stop
    max_queue_wait_s: int     # default 60  — how long a request queues during a swap
    driver_config: {}         # discriminated union, typed per driver (§3)
```

**Why one record (not engine/capability/deployment split):** the engine/model/lifecycle facts scattered today across `LLM_BACKEND_HOSTS`, `TTS_HOST`, `swap_kobold_model`, `ensure_target` all describe **one process**. Splitting them into joined tables is the indirection OpenRouter needs (it curates 300 models) and we don't. We keep the *existing* three-collection split (services / protocol_frontends / workflows) — a protocol frontend is a wire adapter, a workflow is a ComfyUI graph, neither is an engine — and make the **service** record self-sufficient, the way llama-swap's per-model record owns `cmd`+`cmdStop`+`proxy`+`ttl` in one place.


### 1.2 Model file availability contract

Model-file availability is a generic registry projection, not an engine adapter. The gateway checks only files that the registry declares as gateway-host files:

- `model_path`, `path`, `azizam_model_path`, and the companion `mmproj` are legacy gateway-host file fields by default.
- `activation_model` and `activation_mmproj` are checked only when the value is absolute and the service uses a currently host-managed lifecycle driver (`systemd` or `command`), or when `model_file_scope: gateway` is declared on the model, service, or service config.
- `required_model_files` and `required_files` declare additional gateway-host dependencies, such as companion projector files. These requirements inherit through `base_model_id`.
- `model_file_scope: backend` (also `remote`, `container`, or `external`) means the path belongs to the backend's filesystem or naming scope, so the gateway must not classify it by calling `Path.is_file()` on the gateway host.

Rows with a declared gateway-host file that is missing project as unavailable with `reason: Model file is not installed.` Rows without a declared gateway-host file remain unmeasured by this projection.

### 1.3 Three concrete records

#### (a) Remote always-on OpenAI-compatible endpoint — router-only (the common case)

```yaml
services:
  groq:
    name: Groq (cloud)
    connection:
      host: api.groq.com
      port: 443
      scheme: https
      base_path: /openai/v1
    capabilities: [chat, completions]
    secret_refs:
      api_key: ${env:GROQ_API_KEY}
    models:
      expose: all              # cloud preset: advertise whatever the backend lists
      list:
        - public: fast-llama
          upstream: llama-3.3-70b-versatile
        - public: "*"          # wildcard pool — see §2.2 for precedence
          upstream: "*"
```

This is the **whole record**. No `managed`, no `lifecycle`, no `health.endpoint` override, no `priority`. **This is the common case and must stay this short** — the single most important ergonomic decision in the design.

#### (b) Local llama.cpp / Kobold, systemd-managed, idle-unload + model-swap

Today's `swap_kobold_model` (writes `LLAMA_BEE_MODEL` into a systemd drop-in, daemon-reload, restart — `proxy.py:875-963`) becomes **data describing one driver instance**, not ~250 lines of Kobold-specific Python.

```yaml
services:
  beellama:
    name: BeeLlama (Qwen3.6-27B + MTP)
    connection:
      host: 127.0.0.1
      port: 5051
      health: { endpoint: /v1/models, timeout_s: 5, context_window: declared }
    capabilities: [chat, completions, vision]
    input_modalities: [text, image]
    context_window: 32768
    transforms: [qwen3-no-think]          # built-in transform (§4.1) — messages-array surgery
    models:
      expose: mapped
      list:
        - public: default
          upstream: Qwen3.6-27B-UD-Q4_K_XL-mtp.gguf
          activation:
            env:                          # the systemd driver writes ONLY these env lines
              LLAMA_BEE_MODEL:  "/media/.../Qwen3.6-27B-UD-Q4_K_XL-mtp.gguf"
              LLAMA_BEE_MMPROJ: "/media/.../Qwen3.6-27B-mmproj-BF16.gguf"
        - public: rp
          upstream: Huihui-Qwen3.6-27B-abliterated-UD-Q4_K_XL-MTP.gguf
          activation:
            env:
              LLAMA_BEE_MODEL:  "/media/.../Huihui-Qwen3.6-27B-abliterated...gguf"
              LLAMA_BEE_MMPROJ: "/media/.../Qwen3.6-27B-mmproj-BF16.gguf"
    lifecycle:
      driver: systemd
      managed: true
      swap: exclusive                     # single-slot engine; one model in VRAM at a time
      idle_timeout_s: 1800
      drain_timeout_s: 30
      driver_config:
        scope: user                       # user | system  — explicit, load-bearing (§3.3)
        unit: llama-bee.service           # OPERATOR-WRITTEN unit; gateway never authors ExecStart
        dropin: 20-model.conf             # gateway writes ONLY this drop-in's Environment= lines
        swap_strategy: dropin-env-restart # write drop-in → daemon-reload → restart
```

The `activation.env` block is a **flat env dict** the driver writes verbatim as `Environment=KEY=VALUE` lines. The operator can open the drop-in and confirm it contains *only* what they declared — the "gateway never writes ExecStart" contract is auditable, not opaque.

#### (c) ComfyUI image backend

```yaml
services:
  comfyui:
    name: ComfyUI (Image + Video)
    protocol: comfyui
    native_passthrough: false
    connection:
      host: 127.0.0.1
      port: 7890
      health: { endpoint: /system_stats, timeout_s: 5, context_window: off }
    capabilities: [image.generations, image.edits, video.generations]
    input_modalities: [text, image]
    output_modalities: [image, video]
    adapter_config:                       # PROTOCOL config, not lifecycle (§ simplicity fix)
      workflows_dir: ./workflows/images
      video_workflows_dir: ./workflows
      default_workflow: flux2-klein-4b    # was COMFY_DEFAULT_MODEL literal
    models:
      expose: mapped
      list:
        - public: flux2-klein-4b
          upstream: flux2-klein-4b
    lifecycle:
      driver: external                    # ComfyUI runs as its own service; route + idle-track only
      managed: false
```

**Defaults that make (a) trivial:** `enabled: true`, `origin: operator`, `internal: false`, `protocol: openai-compatible`, `native_passthrough: true`, `health.endpoint: /v1/models`, `health.timeout_s: 5`, `health.context_window: probe`, `models.expose: mapped`, `lifecycle.driver: external`, `managed: false`, `priority: 100`, `role: required`, `input/output_modalities: [text]`. A remote endpoint overrides *none* of them except connection + capabilities + key.

---

## 2. CAPABILITY ROUTING

### 2.1 The resolution algorithm (kills every `*_HOST` constant and the `("kobold","tabby")` gate)

An incoming request carries the **endpoint path** and the **model name**. Resolution is deterministic passes over the live registry — no service-id literals anywhere.

```
resolve(endpoint, model, body) -> backend:
  1. ENDPOINT → REQUIRED CAPABILITY            (static table, à la LiteLLM mode)
       /v1/chat/completions      → chat
       /v1/completions           → completions
       /v1/embeddings            → embeddings
       /v1/images/generations    → image.generations
       /v1/images/edits          → image.edits
       /v1/audio/speech          → audio.speech
       /v1/audio/transcriptions  → audio.transcriptions
       /v1/audio/translations    → audio.translations
       /v1/video/generations     → video.generations

  2. CANDIDATE POOL = services where:
       enabled AND NOT internal AND required_capability ∈ capabilities

  3. NARROW BY MODEL  (precedence is explicit — §2.2):
       a. EXACT match: model == some service.models.list[].public or in .aliases
            → keep ONLY those services (exact beats wildcard, always)
       b. else WILDCARD: services whose models.expose=="all" or have a "*" entry
            AND are reachable (so /v1/models can enumerate)  → keep that set
       c. else if model == "" (capability-only call, e.g. STT) → keep whole pool

  4. NARROW BY REQUEST SHAPE  (implicit capability filtering, à la OpenRouter):
       if body has images                  → require "vision" ∈ capabilities
       if body declares tools              → require "tools" ∈ supported_params (if declared)
       if est_tokens > context_window      → drop candidate
            (if context_window is unknown AND request is oversized → §4.2 penalty rule,
             NOT a silent skip)

  5. PICK: sort by (priority asc, then round-robin among equal priority).
       try first HEALTHY candidate; on failure → next in pool (fallback chain).
       empty pool → 503 with the honest reason
         ("no enabled backend serves audio.speech"
          | "model gpt-4o not served by any reachable backend").
```

This deletes audit findings: `LLM_BACKEND_HOSTS` closed dict, the `EMBEDDINGS_HOST`/`TTS_HOST`/`STT_HOST` pins, the `role table backend ∈ ("kobold","tabby")` gate, `ensure_target`'s literal gate, and the `kobold_has_mmproj() → reroute to tabby` hack (it becomes pass-4 vision fallthrough — the router knows "needs vision, this one has it," never "kobold/tabby").

### 2.2 Wildcard precedence (explicit — no magic)

- **Exact model match always beats wildcard**, regardless of priority. If `gpt-4o` is an explicit `public`/`alias` on any service, only those services are candidates; wildcard pools are not consulted.
- **Among wildcard pools**, strict `priority` ordering applies (then round-robin at equal priority).
- **Wildcard requires reachability.** A wildcard service that is unreachable contributes **no** candidates (it can't enumerate `/v1/models` to prove it serves the model). The router treats it as out of pool and falls through — never "try anyway and surface the backend's error."
- **`models.expose: mapped` (default for local)** advertises only explicitly-mapped public names; internal/served-model/LoRA ids stay private. **`expose: all` (cloud presets)** advertises the backend's full catalog. This stops a local vLLM+LoRA instance from leaking internal identifiers to clients.

### 2.3 Role: a health-alarm gate, never a routing gate (verified against current code)

`role` controls **alerting only**, not backend selection (current code uses it exactly this way — `proxy.py:9730-9732`: a `standby` service that is down reports `standby`, not a red alarm):

- `role: required` — alert if this service is down (it's expected up).
- `role: standby` — down-at-idle is expected; suppress the alarm. Common for an idle-unloaded local model.

**Routing always tries the next healthy candidate in priority order, regardless of role.** A `required` local model that is idle-unloaded does **not** 503 the pool when a `standby` cloud fallback is healthy — the router falls through. This is the least-surprising behavior and removes the "capable backend sitting right there but marked standby → mystery 503" trap.

### 2.4 Multi-backend pools, aliasing, fallback, LoRA

- **Multi-backend pool:** two services declaring `embeddings` form a pool automatically — no separate "group" object. *(LiteLLM: same alias = pool.)*
- **Aliasing:** `models.list[].aliases` maps many public names to one deployment, so a local fine-tune can answer to `gpt-4o`. *(vLLM `--served-model-name`.)*
- **Capability fallback:** a vision request whose top backend lacks vision falls through pass 4 to the next vision-capable service. Generalizes OpenRouter's `context_window_fallbacks` to *any* capability mismatch.
- **vLLM LoRA:** one adapter = **one service record** pointing at the same vLLM host, `expose: mapped`, the adapter name as the `public`/`upstream` model. `native_passthrough: true`; vLLM resolves the base+adapter internally. For pre-loading adapters, the `http-load-unload` driver's `load_url` targets vLLM `/v1/load_lora_adapter`. (No `models[].adapter_refs` sub-schema — the existing record shape already expresses it.)

---

## 3. LIFECYCLE DRIVERS

### 3.1 The closed set (five drivers) and the minimal interface

Every driver implements the same interface. **No driver installs, builds, or provisions anything.**

```python
class LifecycleDriver(Protocol):
    def health(svc) -> HealthState        # healthy|degraded|unhealthy|unknown (always available)
    def status(svc) -> ProcessState       # stopped|starting|running|draining|swapping|failed|unmanaged
    def ensure_ready(svc, timeout_s) -> Result   # start if needed, poll health until ready
    def stop(svc) -> Result               # drain (§3.4) then idle-unload / explicit stop
    def swap(svc, deployment) -> Result   # OPTIONAL; only single-slot drivers implement it
    def preflight(svc) -> Warning|None    # read-only check (unit/container/cmd exists?), §3.3
```

| driver | `ensure_ready` | `stop` | `swap` | install? |
|---|---|---|---|---|
| **external** (default) | no-op; `health()` GET only | no-op | — | never |
| **systemd** | `systemctl [--user] start unit`, poll health | `systemctl stop unit` | write drop-in env → `daemon-reload` → `restart` | never (operator wrote the unit) |
| **docker** | `docker start <c>` / `docker compose up -d <svc>`, poll health | `docker stop <c>` / `compose stop` | restart container with new env/command | never (operator built the image / wrote compose) |
| **http-load-unload** | `POST load_url` (+`load_body`), poll health | `POST unload_url` | `POST unload_url` then `POST load_url` (new model) | never |
| **command** | spawn fixed `cmd` (no `${PORT}`), poll health | run `cmd_stop` or SIGTERM | restart with new args | never (operator typed the cmd) |

*(Prior art: `external` = llama-swap `peers`. `systemd` via **subprocess `systemctl`**, matching current code and the simplicity/self-host verdict — not pystemd/D-Bus. `docker` = the table-stakes r/selfhosted pattern. `http-load-unload` = llama.cpp router `/models/load|unload` + TabbyAPI `/v1/model/load|unload` + vLLM `/v1/load_lora_adapter`. `command` = llama-swap `cmd`/`cmdStop`/`check_endpoint`, with the port-allocation responsibility removed.)*

### 3.2 The two current special cases become two driver instances

- **Kobold's systemd-dropin-swap** (~250 lines) → a **`systemd` driver instance** with `swap_strategy: dropin-env-restart`. `swap()` writes the per-model `activation.env` into the operator's drop-in, `daemon-reload`s, `restart`s. "kobold" appears nowhere — it's this record's `unit: llama-bee.service` + `dropin: 20-model.conf`. The `pgrep llama-server --mmproj` discovery is **deleted** — the activation env already declares the mmproj.
- **Tabby's HTTP load/unload** (`swap_tabby_model`) → an **`http-load-unload` driver instance**:
  ```yaml
  lifecycle:
    driver: http-load-unload
    managed: true
    swap: exclusive
    driver_config:
      load_url:   /v1/model/load
      unload_url: /v1/model/unload
      load_body:  { model_name: "${deployment.upstream}" }   # templated, à la KServe {{.Name}}
  ```

### 3.3 Honoring the boundary + scope/auth honesty

- The **systemd** driver writes **only** the drop-in env file it's told to, and runs `systemctl [--user] start/stop/restart` + `daemon-reload`. It never writes `ExecStart`, never creates the base unit, never installs the binary.
- **`scope: user | system` is an explicit modeled field**, because the auth requirements differ and getting it wrong is a *silent* failure (exit-code 5 unit-not-found, or polkit denial):
  - `scope: user` requires the gateway process to run as the user who owns the units, **and** `loginctl enable-linger <user>` so the user manager is reachable when no session is active.
  - `scope: system` requires the gateway to have systemd D-Bus/polkit authority for system units (typically running as root or with a polkit rule).
- **`preflight()` runs at reconcile, read-only** (`systemctl [--user] cat <unit>` / `docker inspect <c>` / `which <cmd>`), and surfaces a **config warning in the admin UI** *before* a request ever hits the missing handle. The honest error ("unit llama-bee.service not found — declare it out-of-band, the gateway drives units it does not author") shows at config time, with a copy-pasteable example unit the operator writes themselves.
- The **docker** driver runs `docker start/stop` or `docker compose up -d/stop` against a named container/service the operator already created. It never `docker run`s a new container from an image tag (that's provisioning existence), never `--pull`s, never builds.
- **Idle-unload** (`idle_timeout_s`) is the gateway's own LRU timer firing `stop()` on the declared handle — Ollama's `keep_alive` grammar (`0`=never, `-1`=pin, `>0`=idle window), with per-request override.

### 3.4 Swap semantics: a real drain contract (was a draft lie)

`swap` has three values:

- **`none`** (default) — no model-swap; the engine serves whatever it has.
- **`exclusive`** — single-slot engine (bare `llama-server`, Tabby): one model in VRAM at a time. Swapping is dangerous to in-flight work, so it has a **drain contract**:
  1. The gateway keeps a per-service `active_requests` counter (incl. open SSE streams).
  2. On swap, the service enters `swap_state: draining`; **no new requests are routed** to it; the gateway waits up to `drain_timeout_s` (default 30) for `active_requests → 0`.
  3. Requests arriving during a swap **queue** up to `max_queue_wait_s` (default 60), then 503 honestly ("model swap in progress").
  4. Once drained, `swap_state: swapping`, the driver executes the swap, polls health, then `running`.
  5. `active_requests`, `swap_state`, `loaded_at`, `expires_at` are exposed on the runtime panel (§6.4).
  This replaces the current per-service module locks (`kobold_swap_lock`/`tabby_swap_lock`, `proxy.py:789,1101`) with a **generic per-service lock + drain**, and fixes the verified gap: today those locks serialize *callers* but never drain *in-flight streams*, so a long generation gets a hard mid-stream error when someone else requests a swap.

  **Status (2026-10):** steps 1–3 shipped as an opt-in, `llm.swap_drain` (`gateway/swap_drain.py`, off by default). Differences from the plan above: requests are counted at the gateway's last hop to the engine (the LiteLLM inner hop and the `current` route), not at the outer route, because chat calls `ensure_target` on both hops and an outer count deadlocks the inner hop; a drain that times out (`timeout_s`, default 120) refuses the swap with 503 + `Retry-After` instead of forcing it; arriving requests queue on the per-service swap lock for the drain plus the swap, with no separate `max_queue_wait_s`; a request takes its lease in the same hold of that lock in which it made its target ready (or, for `current`, read the loaded model); a lease never expires, because a leaked lease cannot be told from a long stream, so `timeout_s` is the only bound and a deferral logs the oldest lease's age. `/v1/status` `swap_state.drain` shows the in-flight count and oldest lease age per backend (read-only); a confirmed leaked lease is cleared only by a gateway restart through `scripts/safe-restart`. Step 5 (runtime panel) is not built.
- **`engine-managed`** — the engine schedules its own model loading/unloading (Ollama `num_parallel`/`keep_alive`, LM Studio, vLLM multi-LoRA). The gateway sends the model name and **does not gate**. Distinct from `external` (which implies one always-on model) — `engine-managed` can still be `managed: true` for idle-unload of the *whole* engine, while never serializing per-model requests.

---

## 4. PROVIDER TRANSFORMS

### 4.1 Declarative primitives + named built-ins (the corrected claim)

Today `/no_think` injection, the `VOICE_MAP`, and reasoning-effort translation are `if model.startswith("qwen")`-style branches scattered across `proxy.py`. Replace them with a **transform registry**: named, ordered, attached to a service or deployment by id.

```python
class Transform(Protocol):
    id: str
    def on_request(body, ctx) -> body
    def on_response(chunk, ctx) -> chunk
```

**Five declarative primitives** (no Python, reviewable from YAML):

```yaml
transforms:
  voxtral-voices:               # was VOICE_MAP + DEFAULT_VOICE
    kind: value-map
    param: voice
    map: { alloy: neutral_female, echo: warm_male }
    default: neutral_female

  drop-logit-bias:              # per-backend "doesn't support X"
    kind: drop-params
    drop: [logit_bias]

  add-default-temp:
    kind: default-params
    set: { temperature: 0.7 }

  rename-stop:
    kind: param-map             # rename / inject a top-level body param
    when: { param: reasoning_effort, equals: none }
    inject_body: { chat_template_kwargs: { enable_thinking: false } }

  system-prefix:                # NEW primitive — needed because the first real
    kind: message-inject        #   example (qwen no_think) is messages-array surgery
    role: system
    text: "/no_think"
    mode: append-or-insert      # append to existing system msg, else insert one
```

**Named built-ins** for behaviors no primitive cleanly expresses. Honest correction to the draft: the `qwen3-no-think` behavior is **not** a `param-map` — verified at `proxy.py:5019-5028`, it does the full sequence (pop+validate `reasoning_effort`, conditionally set `chat_template_kwargs.enable_thinking`, then append `/no_think` to an existing system message or insert a new one). It ships as a **built-in** that internally composes `param-map` + `message-inject`:

```yaml
transforms:
  qwen3-no-think:
    kind: builtin               # richer than a single primitive; reviewable by id + docs
    id: qwen3-no-think
```

There is **no `kind: plugin`**. If a real behavior exceeds the primitives, we add a named built-in (code we review) or a new primitive — never a generic "import any callable" door.

### 4.1.1 Composition & ordering (explicit precedence)

- **Resolution order:** global request-normalizers (declared in static config, e.g. `reasoning_effort` handling that must run *before* routing) → service-level `transforms` → model-level `transforms`.
- **Merge rule:** model-level transforms **replace** a service-level transform **of the same id**; otherwise they **append**. This lets a model opt out of a service-wide inject by redefining/empty-ing that id.
- The design docs ship a "what fires for a `/chat/completions` request, in order" table so operators can reason about conflicts (e.g. a `drop-params` meant to suppress a `param-map` inject must be ordered/scoped correctly).

### 4.2 Context-window source (kept default-on, with staleness fixed)

Set per service/health via `connection.health.context_window`:

1. **`declared`** — operator's `context_window` is authoritative (the effective runtime cap, à la vLLM `--max-model-len`; never GGUF-key spelunking).
2. **`probe`** (**default for openai-compatible**) — on each successful health check the gateway reads the backend's own catalog (vLLM `/v1/models[].max_model_len`, llama-server `n_ctx`) and caches it in the DB. **Re-probes after any swap** and on reconnect, so a swapped model never serves a stale window. The cached value is shown in the UI labeled `(probed)`.
3. **`off`** — no probe (e.g. ComfyUI, where context is meaningless).

**Why default-on (kept against the simplicity reviewer's "drop probing"):** pass-4 oversized-request routing is a *silent no-op* for any service with an unknown context window, and most operators won't hand-declare it. With probing off-by-default, the r/LocalLLaMA "send a 28k request, expect it to route to the 70B not the 7B" case silently fails. Staleness — the reviewer's real objection — is addressed by re-probing on swap/reconnect and by labeling the value's provenance. **Unknown-window handling in pass 4:** if a request is oversized and a candidate's window is unknown, the candidate is **penalized to last in priority** (tried only if nothing with a known-sufficient window exists) and the trace logs `context_window: unknown — penalized for oversized request` — never a silent skip, never a silent route-into-OOM.

---

## 5. CONFIG-AS-CODE vs DB

### 5.1 Honest current state → target (the three-state migration)

The draft claimed "file wins" as if it were the architecture. It is **not** today. Verified: `service_registry.py:10` ("the registry is the runtime source of truth") and `:280` ("The registry (admin.db) is the source of truth"). The honest path is three states, and **the flip is migration Step 2, not last** (§7), so every later step builds on the right authority model:

```
  STATE 1 (legacy):       YAML is import-seed only; after first boot the DB is authoritative,
                          YAML goes stale. ← what the code does TODAY.
  STATE 2 (transition):   DB changes stream-write back to the file (split-file, below);
                          file becomes reviewable and reproducible.   ← migration Step 2.
  STATE 3 (target):       file wins on reconcile; DB is a pure cache. ← end state.
```

**Operators are told explicitly:** until the flip, UI edits live only in the DB and would be lost if a stale YAML later wins. That is the NPM trap the design avoids — by *naming* it and shipping the flip early, not deferring it.

### 5.2 Split-file write-through (fixes the GitOps clobber race)

```
  services.yml          ── operator-owned, git-tracked (the blueprint)
  services.local.yml    ── gateway-owned, gitignored by default (UI writes land here)
        │                       │
        └──── merge: local overrides base, per key ────┐
                                                        ▼
                                            in-memory registry → router
        admin.db (ephemeral ONLY: health history, last_used_at,
                  live VRAM, probed context windows, swap locks/state)
```

- **`services.yml` is operator/git-owned.** A GitOps pull replaces it freely; the gateway never writes to it without an explicit operator action.
- **The UI writes to `services.local.yml`** (gitignored). A "Promote to registry" action merges chosen local changes into `services.yml` for committing. This is Authentik blueprint-vs-manual / ArgoCD base-vs-override — borrowed *with* its separation, not just its name. It removes the race where a 60s reconcile or a git pull silently clobbers a UI edit (or vice-versa).
- **`GET /api/config/export`** always returns the canonical merged YAML that rebuilds the gateway — the single-source-of-truth guarantee that earns GitOps trust (Caddy "export always canonical"; avoids the NPM DB-only trap).
- **`git diff services.yml` stays clean.** No ephemeral state (health, last-used, probed windows) is ever written to either YAML — those live in `admin.db`.
- **Removal = delete the block** (or `enabled: false` to disable without removing). No `state: absent`. On reconcile, a record absent from the merged file is **drained then removed**: it enters `draining` (no new requests), `stop()` after `drain_timeout_s`, then the record is dropped — never a hard mid-request rug-pull.

### 5.3 Hot-reload (deletes the #1 gap)

Two-layer split, à la Traefik **static vs dynamic**:
- **Static** (restart required, rare): listen port, DB path, OIDC, catalog dir. ~5 fields.
- **Dynamic** (hot-reloaded): **everything in `services`** — connection, capabilities, model maps, transforms, lifecycle, idle timeouts, priority.

Reload = file-watch (or `POST /api/config/reload`, or `SIGHUP`) re-runs the loader, rebuilds capability pools, and **swaps the in-memory registry atomically**. Because the router reads pools from the live registry object (not import-time constants), no restart is needed. The import-time `LLM_BACKEND_HOSTS`/`TTS_HOST`/`STT_HOST`/`EMBEDDINGS_HOST` constants — the literal reason a restart is mandatory today (verified `proxy.py:382,521,527,536`) — **cease to exist**; they were always `registry.pool_for(capability)`.

---

## 6. ADMIN UX (screen by screen)

Left-rail sidebar (one section at a time), matching the existing `/admin` shell.

### 6.1 Services (list)

A table. Per row: **name · capabilities (chips) · 4-state health badge** (healthy/degraded/unhealthy/unknown — never a green/red boolean; `unknown` until a check completes) · **lifecycle badge** (`external` / `managed:running` / `managed:idle` / `managed:draining` / `managed:stopped`) · last-used · priority. Aggregate header: "5 services · 3 healthy · 1 idle · 1 unknown." A banner appears **only** when the running config differs from the file (drift), with a one-click "Promote local changes to registry."

### 6.2 Add a service (the flagship flow — three clicks for case (a))

A single form with a **preset picker** (the existing `PROVIDER_PRESETS` — verified at `service_registry.py:503`): vLLM, Ollama, LM Studio, TabbyAPI, ComfyUI, "OpenAI-compatible (custom)."

- **Case (a) remote endpoint:** pick "OpenAI-compatible" → paste base URL → pick `api_key` env-var name → add model rows → **Save**. No lifecycle touched; record defaults to `external`. The secret field accepts **only an env-var name**, never plaintext.
- **Case (b) systemd-managed llama.cpp:** same form, then expand **Lifecycle** → driver `systemd` → `scope` (user/system) + unit + drop-in + `swap: exclusive` + idle timeout → per-model `activation.env` in the model rows. The form runs `preflight()` (read-only `systemctl cat`) and **warns honestly** if the unit/scope is wrong, with a copy-pasteable example unit the operator writes themselves.
- **Case (b′) docker-managed:** driver `docker` → container name or compose service → idle timeout. Preflight `docker inspect`.
- **Case (c) ComfyUI:** pick "ComfyUI" → protocol auto-set to `comfyui`, form swaps to `adapter_config` fields (`workflows_dir`/`default_workflow`), capabilities pre-filled → Save.

On Save: write to `services.local.yml` → reconcile → **live without a restart**. No "restart to apply" banner — that banner was the honesty patch for the gap this design closes.

### 6.3 Capability / model view (the trust surface)

A read-derived screen showing **pools**: for each capability, the ordered list of services serving it, with priority and a **"Test route"** action — enter a model name + endpoint → see exactly which backend resolves and why (the passes from §2.1, shown as a deterministic trace, including wildcard precedence and any context-window penalty). Because routing is strict priority + round-robin (no weighted-random), the trace is a confident "this request goes to backend A," not a probabilistic guess. This is the operator-facing proof that routing is registry-driven, and doubles as the "why did this 503?" debugger.

### 6.4 Lifecycle controls (managed services only)

Per managed service: **Start / Stop / Swap-model** buttons calling `ensure_ready` / `stop` / `swap` on the declared handle. Live panel shows `status` + `swap_state` (idle/draining/swapping), `active_requests`, `loaded_at`, `expires_at` (idle countdown), `vram_bytes` — the Ollama `/api/ps` surface, exposed as `/api/services/{id}/runtime`. A `managed: false` service shows **no** Start/Stop (it's not ours to start) — only health.

---

## 7. WHAT GETS DELETED (the ~74 → mapped)

**Status — 2026-10-10, owner-supplied audit at `fdc3c9d`:** Some named
health wrappers remain. `backend_supports()` is dead code, not an active
config reader. The deletion table and recommendations below are historical
design intent, not proof that every named health function was removed.

| Today (audit category) | Becomes | Survives? |
|---|---|---|
| `TABBY_HOST`/`KOBOLD_HOST`/`TTS_HOST`/`STT_HOST`/`EMBEDDINGS_HOST`/`COMFY_HOST` constants + ports/paths | `service.connection.*` | **config field** |
| `LLM_BACKEND_HOSTS` closed dict | `registry.pool_for(capability)` | **deleted** |
| `backend_for_host()` fallback literal `"kobold"`; `routing.default_backend: kobold` | highest-priority chat backend in pool | **deleted** |
| `ensure_target` gate `backend in ("kobold","tabby")` | `LifecycleDriver.ensure_ready` (external = no-op) | **deleted** |
| role table `backend ∈ ("kobold","tabby")` | pool membership by capability | **deleted** |
| `/v1/embeddings`,`/v1/audio/*` pinned to single host | endpoint→capability→pool resolution (§2.1) | **deleted** |
| `check_tabby_health`/`check_tts_health`/… named fns | one `check_service_health(id)` + `connection.health` | **deleted** |
| `swap_tabby_model` Tabby load/unload protocol | `http-load-unload` driver instance | **lifecycle driver** |
| `swap_kobold_model` drop-in write + daemon-reload + restart | `systemd` driver, `swap_strategy: dropin-env-restart` | **lifecycle driver** |
| `pgrep llama-server --mmproj` discovery | declared `activation.env` | **deleted** |
| `_BACKEND_STATE_STRATEGIES` dict keyed by backend-type names | per-driver `status()` | **deleted** |
| idle-unload timer + `Conflicts=` assumption | `lifecycle.idle_timeout_s` + driver `stop()` + drain | **config + driver** |
| `kobold_swap_lock` / `tabby_swap_lock` module locks | generic per-service lock + drain contract (§3.4) | **generic** |
| `VOICE_MAP` + `DEFAULT_VOICE` | `value-map` transform | **transform** |
| Qwen3 `/no_think` + reasoning_effort (messages-array surgery) | `qwen3-no-think` **built-in** transform (NOT a param-map) | **named built-in** |
| context-window discovery per-engine | `context_window` field + `health.context_window: probe` | **config + health setting** |
| `VISION_MODEL_PATTERNS` substring list | `capabilities: [vision]` + `input_modalities: [image]` | **config field** |
| `detect_model_backend()` filesystem EXL2 scan | operator declares capability/model | **deleted** |
| comfy workflow-by-filename, `COMFY_DEFAULT_MODEL`/`VIDEO_DEFAULT_WORKFLOW` | `adapter_config.default_workflow` + workflows collection | **config + existing catalog** |
| `status_endpoint._SERVICE_UNITS` process-name list | derive from `lifecycle.driver_config.unit`/`container`/`cmd` | **deleted** (registry-derived) |
| machine-specific absolute paths (tabbyapi configs, gpu_budget.sh, Daisy) | `${env:...}` / `${file:...}` | **config field (ref)** |
| x-api-key/x-admin-key injection special-cased on `service=="tabby"` | `secret_refs` + header policy on the record | **config field** |
| `kind: provider/llm-substrate` in operator YAML | internal `internal: true` flag (substrate only) | **internal flag** |
| `source` 5-value enum | `origin: operator/discovered` + free-text `created_via` | **simplified** |

**What should NOT survive, named:** every `if service_id == "..."` / `if backend in (...)` branch; every `*_HOST` import-time constant; every `check_<service>_health` named function; the `pgrep`/filesystem-scan discovery; the per-service swap-lock keys; `routing.default_backend`; the `${PORT}` macro idea; `kind: plugin`; `state: absent`; `weight`. **Intentionally surviving:** the LiteLLM-substrate row's editability lock (the gateway spawns that process — verified `service_registry.py:421-423`), now keyed off `internal: true`; and the three-collection split (services / protocol_frontends / workflows).

---

## 8. MIGRATION (no broken deploys; corrected order)

The current code is already partway: generic health exists, `LLM_BACKEND_HOSTS` is derived from `config["backends"]`, `vision_capable_llm_backends()` exists, and the registry persists records to `admin.db`. This is *finishing an in-flight migration*. Each step is independently shippable and leaves the gateway working.

**Step 0 — Generalize the swap lock first (one-hour safety fix).** Replace the named `kobold_swap_lock`/`tabby_swap_lock` (`proxy.py:789,1101`) with a `service_id → Lock` dict *before any routing change*. No behavior change, but it makes every subsequent step that touches routing automatically get per-service swap serialization. (The power-user/self-host reviewers flagged that steps which modify routing before the lock is generic open a concurrent-swap data hazard.)

**Step 1 — Hot reload + live registry (deletes A3, the #1 gap).** Make the router read pools from the live in-memory registry; add `POST /api/config/reload` + file-watch; delete the import-time `*_HOST` constants. *(Reordered ahead of capability dispatch: dispatch can't be meaningful while pools are import-time constants — verified `proxy.py:382,521-536`. Steps 1+2 are one milestone.)* **Test:** add a service via API and route to it with **no restart**.

**Step 2 — Source-of-truth flip + capability dispatch (the load-bearing decision, moved early).** Implement split-file write-through (`services.yml` git-owned, `services.local.yml` UI-owned), `GET /api/config/export`, and the reconcile loop — the file now wins. *In the same milestone*, replace the `/v1/embeddings` and `/v1/audio/*` host pins with `resolve(endpoint, model)` (§2.1). Today each capability pool has one member, so behavior is bit-identical — but a *second* embeddings/TTS backend now works, and operators who edit YAML get the benefit immediately instead of waiting until the end. **Test:** golden-trace parity for existing routes; a synthetic second embeddings backend gets traffic; edit `services.yml` → `git diff` shows it → rebuild from file reproduces the gateway.

**Step 3 — `LifecycleDriver` with `external` + `docker` only.** `external.ensure_ready` is a no-op health check (makes "always-on new engine" activate instead of 503-ing at the literal gate); `docker` covers the common containerized backend. **Test:** a new always-on vLLM serves chat with no code change; a docker-run Ollama idle-unloads via `docker stop`.

**Step 4 — Role/pool reads + role-as-alarm.** Drop the `("kobold","tabby")` role filter; validate roles against capability pools; confirm `role` is alarm-only (it already is — `proxy.py:9730`). **Test:** point a role at a new backend; it routes. A standby backend down at idle does not 503 the pool.

**Step 5 — Transforms registry.** Extract scattered branches into the five primitives + `qwen3-no-think`/`voxtral-voices` built-ins. **Test:** golden request/response parity vs the old hardcoded path (especially the `/no_think` messages-array surgery).

**Step 6 — `systemd` + `http-load-unload` drivers + drain contract (the genuinely service-shaped work, last).** Port `swap_kobold_model` into `systemd`, `swap_tabby_model` into `http-load-unload`, each as a record instance; implement the §3.4 drain (active-request counter, `swap_state`, queue). Keep the old functions behind a feature flag until each passes a live-machine swap golden-test. **Test:** swaps produce identical drop-in/HTTP calls; an in-flight stream drains before swap instead of erroring; idle-unload fires `stop()` on schedule.

**Throughout:** the `runtime_backend_from_service` projection stays the seam — legacy execution paths keep reading the flat `config["backends"]` dict while the registry shape evolves underneath, so no step rewrites the ~19k-line router core at once.

---

## 9. Explicitly deferred / out of scope (v1.1+)

- **`kind: plugin` transforms.** Deferred until a real behavior exceeds the five primitives + built-ins *and* a plugin design exists (sandboxing, versioning, discovery, where they live). Until then, "we don't have a primitive for that" → add a reviewable named built-in, not an arbitrary-code door.
- **Weighted load-balancing (`weight`).** v1 is strict priority + round-robin at equal priority (keeps the Test-route trace deterministic). Weighted/random selection is a v1.1 addition *if* an operator demonstrates a homogeneous-backend need — and it must come with a "trace shows priority order, actual pick may vary" honesty note.
- **Provisioning existence.** Out of scope **permanently**, by axiom: no installing engines, downloading weights, authoring `ExecStart`, `docker run`-from-image, or building. The gateway drives operator-declared handles only. Operators who want process *supervision* (port allocation, crash-loop restart) run llama-swap/systemd/docker for that layer and point the gateway at the result as an `external` (or `engine-managed`) service.
- **Cross-host scheduling / GPU placement.** The gateway routes and idle-unloads; it is not a cluster scheduler. Multi-GPU budget logic (today's `gpu_budget.sh`) is an operator concern surfaced via `${env:...}`, not modeled.
- **Auto-discovery of new engines on the network.** `origin: discovered` exists for records the gateway populates from a backend's own catalog, but network scanning for engines is deferred — operators declare their endpoints.

---

The final design is saved at `/home/tnunamak/applications/azizam/docs/design/ENGINE_REGISTRY_DESIGN.md`.
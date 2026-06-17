# Lifecycle Driver Port — kobold/tabby swap → registry-driven drivers

**Status:** Architect plan (no code written yet).
**For:** the Vivid Fish AI Gateway.
**Implements:** `ENGINE_REGISTRY_DESIGN.md` §3 (lifecycle drivers) + §6 (the service-shaped swap work) + Migration Step 6.
**Foundation in place:** `gateway/lifecycle_drivers.py` (`LifecycleDriver` Protocol, `ExternalDriver`, `DockerDriver`, `driver_for_service`); `proxy.swap_lock_for()` generic per-service lock.
**Non-negotiable:** BEHAVIOR-IDENTICAL for the current kobold/tabby deployment. The legacy code stays until a golden test proves the driver path byte-identical, behind a feature flag.

---

## 0. What I verified before designing (corrections to the behavior map)

I read the actual code at every cited line. The behavior map's line numbers were stale (it cited 1520/1560/20070; reality is 1473/3111/3619). More importantly, **three load-bearing facts differ from the map and the design doc, and the port MUST honor the code, not the doc:**

1. **The drop-in env keys are `LLAMA_BEE_MODEL` / `LLAMA_BEE_MMPROJ`** (proxy.py:1265-1266), NOT `LLAMA_TURBO_MODEL` as the docstring (1221) and `ENGINE_REGISTRY_DESIGN.md` example (line 191) claim. The example unit in the design doc would be a **silent swap-breakage** if seeded verbatim. The systemd driver writes whatever `activation.env` declares; the **seed** must declare `LLAMA_BEE_MODEL`/`LLAMA_BEE_MMPROJ` to match the operator's actual `llama-bee.service` ExecStart. This is exactly the kind of byte-difference the golden test exists to catch.

2. **The tabby load body is NOT a static template.** `swap_tabby_model` (proxy.py:1415-1416) calls `get_tabby_model_config()` at swap time, reads `model_params` (`max_seq_len`, `cache_size`, `cache_mode`, `gpu_split_auto`, `autosplit_reserve`, `max_batch_size`, `chunk_size`) live from tabby's YAML config file, and merges `{model_name: target, **model_params}`. A static `load_body: {model_name: "${deployment.upstream}"}` (design §3.2 line 355) is **NOT identical** — it drops the live params. The `http-load-unload` driver needs a `load_body_extra` source ("merge these live params") OR the seed must snapshot the params. See §3.2 and the riskItem in §8.

3. **`_LOCAL_SWAP_BACKENDS` and `apply_tabby_auth` are NOT lifecycle branches.** They gate the GGUF-alias catalog (592/610/16662/16673) and inject `x-admin-key`/`x-api-key` (2527). They do not belong to swap and are NOT ported by this work — see §6 (non-lifecycle disposition).

Single dispatch point confirmed: **`ensure_target(backend, model, mmproj)` at proxy.py:1473 is the ONLY swap entry**, called once from proxy.py:3111. The driver path for non-kobold/tabby already exists (1496-1506). So the port surface is small and surgical: replace the `backend == "kobold"` / `backend == "tabby"` branches (1520, 1560) with `driver.swap(...)` dispatch. There are not "24 swap sites" — there is one, plus 24 *other* engine-literal residues most of which are non-lifecycle (§6).

---

## 1. Target architecture (one dispatch, two new drivers)

```
ensure_target(backend, model, mmproj)            ← proxy.py:1473, sole swap entry
  │
  ├─ activation = activation_for(backend, model) ← resolve per-model activation block from registry
  │                                                  (env dict for systemd, load params for http)
  ├─ driver = driver_for_service(backend, config["backends"])
  │
  └─ if driver.supports_swap() and model:
        await driver.swap(svc, activation)        ← drain → swap → poll → record
     else:
        await driver.ensure_ready(svc, timeout)   ← existing external/docker path
```

`"kobold"` becomes a **SystemdDriver instance** configured by data:
`lifecycle.driver_config = {scope: user, unit: <KOBOLD_SERVICE_NAME>, dropin: 20-model.conf, swap_strategy: dropin-env-restart, health_endpoint: /v1/models}`.
`"tabby"` becomes an **HttpLoadUnloadDriver instance** configured by data:
`lifecycle.driver_config = {load_url: /v1/model/load, unload_url: /v1/model/unload, admin_header: x-admin-key, load_body_params: tabby-config}`.

The strings `"kobold"` and `"tabby"` appear in the **seed data only**, never in driver code.

---

## 2. The `swap()` protocol addition

`LifecycleDriver` (gateway/lifecycle_drivers.py:62-88) gains one **optional** method, plus a capability probe so the dispatcher can branch without engine literals:

```python
@runtime_checkable
class LifecycleDriver(Protocol):
    # ... existing ensure_ready / stop / status / health / preflight ...

    async def swap(self, svc: Mapping[str, Any], activation: Mapping[str, Any]) -> bool:
        """OPTIONAL — single-slot drivers only. Make `activation` the loaded model.

        `activation` is the resolved per-model block:
          - systemd:          {"env": {KEY: VALUE, ...}}
          - http-load-unload: {"model_name": str, "params": {...}}
        Returns True iff the engine is serving the target after the swap.
        ExternalDriver / DockerDriver DO NOT implement this (no single slot).
        """
        ...
```

Dispatch must not `hasattr`-sniff a literal. Add a tiny helper on each driver:

```python
def supports_swap(driver) -> bool:
    return getattr(driver, "_SWAPPABLE", False)
```

`SystemdDriver._SWAPPABLE = True`, `HttpLoadUnloadDriver._SWAPPABLE = True`, others `False`/absent. The dispatcher reads the flag — no `isinstance(driver, SystemdDriver)`, no `backend in (...)`.

`driver_for_service` (lifecycle_drivers.py:359-397) extends `_DRIVER_MAP`:

```python
_DRIVER_MAP = {"external": ExternalDriver, "docker": DockerDriver,
               "systemd": SystemdDriver, "http-load-unload": HttpLoadUnloadDriver}
```

Unrecognized still falls back to `ExternalDriver` (fail-safe, unchanged).

---

## 3. The two new drivers (data-configured, no engine literals)

Both read **only** from `svc["lifecycle"]["driver_config"]` and the passed `activation`. Neither knows what "kobold" or "tabby" is.

### 3.1 SystemdDriver (`swap_strategy: dropin-env-restart`)

`driver_config` schema:

| key | meaning | kobold seed value |
|---|---|---|
| `scope` | `user` \| `system` → `--user` flag | `user` |
| `unit` | operator-written unit name | `${KOBOLD_SERVICE_NAME}` (`koboldcpp.service`) |
| `dropin` | drop-in filename the driver writes | `20-model.conf` |
| `swap_strategy` | only `dropin-env-restart` today | `dropin-env-restart` |
| `health_endpoint` | poll target after restart | `/v1/models` |
| `health_timeout_s` | poll budget | `SWITCH_TIMEOUT` |

Methods (each must reproduce the legacy side-effect sequence captured in §4):

- **`ensure_ready(svc, timeout_s)`** — `systemctl [--user] start <unit>` (only if not running), poll `health_endpoint` until 200 or timeout. (Legacy `switch_backend()` parity; for the current single-engine deployment with `Conflicts=`, "start" is the cold-start.)
- **`stop(svc)`** — `systemctl [--user] stop <unit>` (idle-unload path; legacy has no explicit kobold stop today, so this is additive and gated by `idle_timeout_s > 0`, default off → behavior-identical when unset).
- **`swap(svc, activation)`** — the heart. Reproduces `swap_kobold_model` EXACTLY:
  1. `scope`-aware path: `dropin_path = ~/.config/systemd/<scope>/<unit>.d/<dropin>` (user) — matches `KOBOLD_MODEL_DROPIN` (proxy.py:1020). System scope → `/etc/systemd/system/<unit>.d/<dropin>`.
  2. Build drop-in text from `activation["env"]` **in declared order**, with the **exact** legacy header comment and `[Service]` block and `Environment="KEY=VALUE"` quoting (proxy.py:1262-1267). The driver writes the dict verbatim — so `activation.env = {LLAMA_BEE_MODEL: path, LLAMA_BEE_MMPROJ: mmproj_or_empty}` reproduces it byte-for-byte.
  3. `mkdir(parents=True, exist_ok=True)`; `write_text(...)`.
  4. On write failure → return False (no reload), matching 1268-1270.
  5. Run, in order: `systemctl [--user] daemon-reload`, then `systemctl [--user] restart <unit>` (1278-1281). On any non-zero, remove the drop-in and return False (1290).
  6. Reset the loaded-state cache to unknown (the driver owns a generic per-service state cache replacing `_kobold_state`, see §3.3).
  7. Poll `health_endpoint` up to `health_timeout_s`; on 200, set state authoritatively, `firehose_metrics.record_event("model_swap", ...)`, return True (1298-1319). On timeout, remove the drop-in and return False (1324-1325).

**Validation parity (must live where the legacy does):** the GGUF-absolute-path + mmproj-exists checks (proxy.py:1233-1243, 1521-1531) are part of the *kobold activation resolution*, not generic systemd. They move into `activation_for(...)` (the registry projection that builds the `env` dict) as a **systemd `dropin-env-restart` precondition hook**, NOT into the generic driver — a system-scope unit swapping a non-GGUF engine has no `.gguf` concept. Honest placement: a `precheck` list in `driver_config` (e.g. `precheck: [{file_exists: "$env.LLAMA_BEE_MODEL", suffix: ".gguf", absolute: true}, {file_exists: "$env.LLAMA_BEE_MMPROJ", optional: true}]`) that the driver evaluates generically against the resolved env. This keeps "must be an absolute existing .gguf" as *data* describing the kobold instance, not code that says "kobold". If this generic precheck proves too fiddly to make byte-identical, fall back to keeping the kobold path validation in `activation_for` and document it (see §8 risk K2).

### 3.2 HttpLoadUnloadDriver

`driver_config` schema:

| key | meaning | tabby seed value |
|---|---|---|
| `load_url` | POST to load | `/v1/model/load` |
| `unload_url` | POST to unload | `/v1/model/unload` |
| `admin_header` | header carrying admin creds | `x-admin-key` |
| `load_body_params` | how to fill the load body beyond `model_name` | `tabby-config` (see below) |
| `load_timeout_s` | load POST timeout | `TABBY_LOAD_TIMEOUT` |
| `unload_timeout_s` | unload POST timeout | `60` |
| `success_marker` | substring proving success in stream | `"status":"finished"` |
| `verify_endpoint` | fallback GET to confirm loaded model | `/v1/model` |

Methods (reproduce `swap_tabby_model` EXACTLY):

- **`ensure_ready`** — POST `load_url` with the default model body, poll health. (Tabby's cold path; for the current deployment idle-reload already handles this, so `ensure_ready` is gated and additive.)
- **`stop`** — POST `unload_url` with `admin_header`.
- **`swap(svc, activation)`**:
  1. `current = GET verify_endpoint` (`.id`) with `x-api-key` (proxy.py:1362-1376). If `current == activation["model_name"]` → return True (1391-1392).
  2. If `current`: POST `unload_url` with `{admin_header: ADMIN_KEY}`, 60s; tolerate non-200 with a log; on exception return False (1399-1411).
  3. Build body = `{"model_name": target, **params}` where **params come from `load_body_params`** (1415-1416).
  4. POST `load_url` with `{admin_header: ADMIN_KEY, Content-Type: application/json}`, `json=body`, `load_timeout_s`; stream-decode the response (1417-1427).
  5. Success if `200 and success_marker in response_text` → record + True (1429-1442).
  6. Else if `"status":"error"` or `"error":` in text → False (1444-1446).
  7. Else fallback: `GET verify_endpoint`; if loaded == target → record(verified) + True (1448-1464); else False (1466-1467).

**The `load_body_params` problem (the real one).** Tabby reads live params from its own YAML at swap time via `get_tabby_model_config()`. Three honest options, in order of preference:

- **(A) Generic "params source" callback.** `load_body_params: tabby-config` maps to a registered, named params-provider the driver calls: `params = params_provider(svc)`. The provider for tabby is `get_tabby_model_config()[2]`. This keeps live-read parity. The provider registry is engine-named in its *registration key* but the driver only sees `load_body_params: <name>` (data) and calls `PARAMS_PROVIDERS[name](svc)`. This is the cleanest path that stays byte-identical. **Recommended.**
- **(B) Snapshot at seed time.** Bake the current `model_params` into `driver_config.load_body` once. **Rejected:** not identical — a later edit to tabby's YAML would silently diverge from legacy (which re-reads every swap).
- **(C) Declare params in the registry** as `activation.load_body`. Loses the single-source-of-truth (operator already declares them in tabby's config); duplicative and drift-prone. Deferred.

Go with (A). If the params-provider indirection is judged to leak an engine name into the registry vocabulary unacceptably, document (A) as the chosen compromise (the *name* is data; the *behavior* is a reviewed, registered function — same pattern as the `qwen3-no-think` named built-in in ENGINE_REGISTRY_DESIGN §4.1). See §8 risk T1.

### 3.3 Generic loaded-state cache (replaces `_kobold_state`)

`swap_kobold_model` mutates a module-global `_kobold_state` dict (proxy.py:1044) and reads via `currently_loaded_kobold_model()/_mmproj()` (1204-1215), seeded by `_BACKEND_STATE_STRATEGIES` (1059). For byte-identical behavior the SystemdDriver needs the same "skip if already loaded" + "authoritative set after swap" semantics. Port to a **per-service state dict keyed by `service_id`** living on the driver layer:

```python
_loaded_state: dict[str, dict] = {}   # service_id → {"model","mmproj","ts"}
```

The seed strategy (`_BACKEND_STATE_STRATEGIES`, `_resolve_dot_path`, the `mmproj_pgrep` fallback) moves alongside as a **generic state-probe**, configured by `driver_config.state_probe` (`{endpoint, model_key, mmproj_key, mmproj_pgrep}`), so "ask the engine what's loaded" is data, not a kobold function. **Honest caveat:** the `pgrep llama-server --mmproj` discovery (proxy.py:1106-1133) is the one genuinely engine-shaped probe. ENGINE_REGISTRY_DESIGN §7 says to **delete** it (the declared `activation.env` already carries mmproj). For byte-identical *startup* seeding it must survive the port (the cache is cold at boot, before any swap). Disposition: keep it behind `state_probe.mmproj_pgrep: true` as a named, data-gated fallback for this one instance, and delete it only once the post-swap authoritative-set path has fully replaced cold-seed reliance. Flag as risk K3.

---

## 4. BEHAVIOR-IDENTICAL PROOF strategy (golden side-effect capture)

A botched port doesn't fail a unit test — it breaks live model-swapping. So the proof is a **golden side-effect test**: drive the driver with the kobold/tabby seed and assert the EXACT sequence of side-effects the legacy function produced, captured as ordered, typed records.

### 4.1 The capture harness

Add `tests/test_lifecycle_swap_golden.py`. A `SideEffectRecorder` patches the four side-effect surfaces and appends typed events in order:

- **subprocess**: patch `asyncio.create_subprocess_exec` → record `("exec", argv)`.
- **filesystem**: patch `Path.write_text` / `Path.mkdir` / `Path.unlink` on the drop-in path → record `("write", path, text)`, `("mkdir", path)`, `("unlink", path)`.
- **HTTP**: patch `httpx.AsyncClient` → record `("http", method, url, headers_subset, json_body)` and return scripted responses.
- **metrics**: patch `firehose_metrics.record_event` → record `("metric", name, *args, kwargs)`.

### 4.2 The golden assertions

**Kobold swap golden** — call BOTH `proxy.swap_kobold_model("/m/a.gguf", "/m/p.gguf")` (legacy) and `SystemdDriver().swap(kobold_svc, {"env": {"LLAMA_BEE_MODEL": "/m/a.gguf", "LLAMA_BEE_MMPROJ": "/m/p.gguf"}})` (new) under the same recorder + scripted-healthy HTTP, and assert the two event lists are **equal**. The decisive assertions:
- drop-in `write` text is **byte-identical**, including the `# Managed by openai-proxy...` header, `[Service]`, and `Environment="LLAMA_BEE_MODEL=..."` / `Environment="LLAMA_BEE_MMPROJ=..."` lines (this is where the `LLAMA_TURBO` vs `LLAMA_BEE` trap dies).
- exec sequence == `[("exec", ["systemctl","--user","daemon-reload"]), ("exec", ["systemctl","--user","restart","koboldcpp.service"])]`.
- health poll hits `{KOBOLD_HOST}/v1/models`.
- failure paths: scripted non-zero `restart` → both remove the drop-in and return False; scripted health-timeout → both remove the drop-in and return False.
- empty `mmproj` → `Environment="LLAMA_BEE_MMPROJ="` (empty), matching the text-only contract (proxy.py:1244).
- `metric` event args equal (`backend="kobold"`, `target`, rounded `elapsed_s` — pin time via `monkeypatch` on `time.time`).

**Tabby swap golden** — call `proxy.swap_tabby_model("model-X")` vs `HttpLoadUnloadDriver().swap(tabby_svc, {"model_name": "model-X"})`. Assert:
- when a model is loaded: an unload POST to `/v1/model/unload` with `x-admin-key`, then a load POST to `/v1/model/load` with `x-admin-key`+`Content-Type`, body `{"model_name": "model-X", **model_params}` where `model_params` is the live `get_tabby_model_config()[2]` (proves option A keeps parity).
- success-marker path, error-marker path, and verify-fallback path each produce identical return + identical `record_event` args.
- already-loaded short-circuit issues **zero** HTTP load/unload.

### 4.3 What "identical" must tolerate

Time/elapsed differs run-to-run → pin `time.time`. The state-cache object identity differs (`_kobold_state` vs `_loaded_state["kobold"]`) → assert on *values* (model/mmproj), not the container. Everything else must be exactly equal.

---

## 5. Registry seed (current deployment behaves identically)

`gateway/service_registry.py` lifecycle normalization (lines 414-418, 455-459) currently emits only `{type, managed, service_name}`. Extend `normalize_service_record` / the seed normalizer to the full §1 schema: `{driver, managed, idle_timeout_s, swap, drain_timeout_s, max_queue_wait_s, driver_config}`, with **back-compat**: a record carrying legacy `{type, service_name}` and no `driver` maps `type→driver` (`external`→`external`) and `service_name→driver_config.unit` so existing DB rows keep working.

The kobold and tabby seed records (whatever path projects `config["backends"]["kobold"|"tabby"]`) get:

```yaml
kobold:
  lifecycle:
    driver: systemd
    managed: true
    swap: exclusive
    idle_timeout_s: 0          # OFF → no new idle-stop behavior (identical to today)
    drain_timeout_s: 30
    driver_config:
      scope: user
      unit: koboldcpp.service          # = KOBOLD_SERVICE_NAME (routing.kobold_service)
      dropin: 20-model.conf
      swap_strategy: dropin-env-restart
      health_endpoint: /v1/models
      state_probe: { endpoint: /props, model_key: model_path, mmproj_pgrep: true }
      precheck: [{ file_exists: "$env.LLAMA_BEE_MODEL", suffix: ".gguf", absolute: true },
                 { file_exists: "$env.LLAMA_BEE_MMPROJ", optional: true }]
  # per-model activation built by activation_for():
  #   env: { LLAMA_BEE_MODEL: <gguf path>, LLAMA_BEE_MMPROJ: <mmproj or ""> }

tabby:
  lifecycle:
    driver: http-load-unload
    managed: true
    swap: exclusive
    idle_timeout_s: 0
    drain_timeout_s: 30
    driver_config:
      load_url: /v1/model/load
      unload_url: /v1/model/unload
      admin_header: x-admin-key
      load_body_params: tabby-config      # → PARAMS_PROVIDERS["tabby-config"] = get_tabby_model_config()[2]
      load_timeout_s: 300                  # TABBY_LOAD_TIMEOUT
      success_marker: '"status":"finished"'
      verify_endpoint: /v1/model
```

`idle_timeout_s: 0` is the keystone of behavior-identity: it means the driver's additive `stop()`/idle path **never fires**, so the only new behavior is the swap dispatch, which the golden test pins to identical.

---

## 6. Per-branch migration + non-lifecycle disposition

### 6.1 The lifecycle branches (port these)

| site | legacy | ported to |
|---|---|---|
| proxy.py:1520 `if backend == "kobold"` | inline GGUF validate + `swap_kobold_model` | `activation_for("kobold", model)` + `SystemdDriver.swap()` |
| proxy.py:1560 `elif backend == "tabby"` | `swap_tabby_model` | `HttpLoadUnloadDriver.swap()` |
| proxy.py:1496-1506 (already driver path) | `ensure_target` non-kobold/tabby | unchanged; the kobold/tabby branches collapse INTO it |

After the port, `ensure_target` has **no engine literal** — it resolves a driver, asks `supports_swap()`, and calls `swap()` or `ensure_ready()`. The `if backend not in ("kobold","tabby")` guard (1496) is deleted; all backends take one path.

### 6.2 Non-lifecycle residue — honest disposition (NOT this PR)

These were in the behavior map's `nonLifecycle` list. None is a swap branch; forcing them into a driver would be wrong. Disposition:

| site | what it is | disposition |
|---|---|---|
| 592/610/16662/16673 `_LOCAL_SWAP_BACKENDS` | gates the **GGUF-alias catalog** (which models surface a `path`) | **Separate cleanup.** Generalize to "any backend whose driver is systemd `dropin-env-restart`" (i.e. read `driver_config.swap_strategy`), not a hardcoded set. Becomes stale only AFTER kobold/tabby are seeded — do it in the same PR's cleanup commit, guarded by the same flag. |
| 2527 `apply_tabby_auth` + 2559 `if service == "tabby"` | injects `x-admin-key`/`x-api-key` per request path | **Generalize via a registry field**, not a driver. Add `connection.auth_inject: {admin_header, api_header, admin_paths: [...]}` to the record; `prepare_downstream_headers` reads it. Distinct, smaller cleanup — list it, don't force it into swap. |
| 3002-3060 default/upstream model resolution; 3062 kobold path validation | request-time model resolution | **Out of scope for the swap port.** Belongs to the routing/transforms migration (Steps 2/5). The 3062 GGUF check overlaps the `precheck` in §3.1 — converge there later. |
| 3165/3177 vision checks; 9293-9316 dynamic roles; 16534/16630 context_window; 16662-16673 role catalog; 19690 auth classes | routing/capability/auth | **Not lifecycle.** Owned by Steps 2/4/5. Named here for honesty; untouched by this PR. |
| 3619 `restart_tabby_if_needed` / `TABBY_MANAGED` process spawn (342, 20096-20109) | spawns a tabby **process** if `TABBY_MANAGED=true` | **AXIOM-0 VIOLATION, separate fix.** This is the gateway *owning a process's existence* (start/stop/restart-on-crash), which §0 axiom 2 forbids. It must become `external` (operator runs TabbyAPI; gateway drives load/unload only). This is NOT a swap port — it's a removal. Doing it inside the swap PR would conflate two risky changes. **Recommend a dedicated follow-up** with its own parity proof (the http-load-unload driver makes tabby externally-managed possible; flipping `TABBY_MANAGED` default to false is the actual cut). See §8 risk T2. |

---

## 7. Feature flag + rollback

A single boot flag `AGW_DRIVER_SWAP` (env / config `routing.driver_swap`), default **off** (legacy):

```python
# in ensure_target(), at the kobold/tabby branch site
if DRIVER_SWAP_ENABLED:
    activation = activation_for(backend, model, mmproj)
    driver = driver_for_service(backend, config.get("backends"))
    if supports_swap(driver) and model:
        return await driver.swap(backends[backend], activation)
    return await driver.ensure_ready(backends[backend], timeout_s=HEALTH_CHECK_TIMEOUT)
else:
    # ── LEGACY PATH (unchanged kobold/tabby branches) ──
    ...
```

- **Off (default):** byte-for-byte today. Ships safely.
- **On:** driver path. Enabled only after the §4 golden tests are green AND a live-machine smoke swap on Tim's running kobold/tabby is confirmed.
- **Rollback:** flip the flag; zero redeploy of code needed. Legacy functions (`swap_kobold_model`, `swap_tabby_model`, `_kobold_state`, helpers) are **NOT deleted** until the flag has run as default-on through at least one real swap cycle per engine. The DELETE is a separate, later commit — explicitly gated on "verify phase proved identical," per the binding non-negotiable.

Roll-forward order: (1) land drivers + protocol + seed + golden tests, flag OFF. (2) Suite green at ≥1971. (3) Flip flag ON in Tim's deployment, smoke-swap both engines. (4) Soak. (5) Delete legacy + the `_LOCAL_SWAP_BACKENDS`/`apply_tabby_auth` cleanups. (6) Remove the flag.

---

## 8. Honest risk list — items that cannot be cleanly/fully ported

| id | item | why it resists clean port | recommendation |
|---|---|---|---|
| **K1** | Drop-in env keys are `LLAMA_BEE_*`, docs say `LLAMA_TURBO_*` | the design doc's example unit is WRONG vs live code; seeding it verbatim silently breaks swap | Seed `LLAMA_BEE_MODEL`/`LLAMA_BEE_MMPROJ`; golden test asserts byte-identical drop-in text. **Fix the design doc.** Resolved by §4.2. |
| **K2** | GGUF absolute-path + mmproj-exists validation | engine-shaped ("must be `.gguf`"); a generic systemd unit has no GGUF concept | Express as `driver_config.precheck` data (§3.1). If generic precheck can't be made byte-identical, keep validation in `activation_for("kobold")` and document it as a kobold-instance precondition — do NOT fake-generalize it. |
| **K3** | `pgrep llama-server --mmproj` cold-seed | the one genuinely process-introspecting probe; design §7 says delete it | Keep behind `state_probe.mmproj_pgrep: true` (data-gated) until post-swap authoritative-set fully replaces cold-seed reliance; then delete. Not deletable in the same PR without risking stale cold-start state. |
| **T1** | Tabby load body needs **live** `model_params` | static `load_body` template (design §3.2) is NOT identical — it drops live YAML params | Use a named params-provider (option A, §3.2). The *name* is data; the function is reviewed. If even that is rejected, this is the honest minimum indirection — report it, don't ship a divergent static body. |
| **T2** | `TABBY_MANAGED` process spawn / `restart_tabby_if_needed` | gateway owns the tabby **process** (start/restart-on-crash) — axiom-0 violation | NOT a swap port; a separate removal that flips tabby to external. **Do not bundle** into the swap PR. Dedicated follow-up with its own proof. |
| **T3** | `apply_tabby_auth` `x-admin-key` injection on `/v1/models` & `/v1/model/list` | per-request auth, not lifecycle; tabby-literal at proxy.py:2559 | Generalize via `connection.auth_inject` registry field (§6.2), separate cleanup. The http-load-unload driver already needs `admin_header` for swap, but request-path auth is a different surface. |
| **DRAIN** | drain contract (§3.4: active-request counter, `swap_state`, queue) | legacy locks serialize callers but never drain in-flight streams; adding drain is a **behavior change**, not identity | Ship drivers with `swap_lock_for` parity FIRST (identical to today: serialize, no drain). Add the drain contract as an **opt-in** `drain_timeout_s`/`max_queue_wait_s` step AFTER parity is proven — it's an improvement, and must not ride in the "behavior-identical" commit. The golden test asserts the no-drain path matches legacy. |

---

## 9. Commit sequence (each green, each reversible)

1. **Protocol + dispatch helper** — add `swap()` to the Protocol, `supports_swap()`, extend `_DRIVER_MAP`; no behavior change (drivers not yet wired). Suite green.
2. **SystemdDriver** + golden kobold test (drives driver directly; legacy still in `ensure_target`). Green.
3. **HttpLoadUnloadDriver** + golden tabby test (with params-provider). Green.
4. **Seed** kobold/tabby lifecycle records; registry lifecycle-schema extension + back-compat. Green (records present, unused while flag off).
5. **Wire `ensure_target` behind `AGW_DRIVER_SWAP`** (default off). Golden parity test toggles the flag and asserts identical ensure_target outcomes. Green.
6. *(after live smoke-swap soak)* **Delete legacy** + generalize `_LOCAL_SWAP_BACKENDS` (→ read `swap_strategy`) + remove flag.
7. *(separate PRs)* drain contract (DRAIN); tabby→external (T2); `auth_inject` (T3).

Never touch ports 5000/15000. Never print secrets (the golden HTTP recorder captures header **names**, not values — assert `x-admin-key` present, never its content).

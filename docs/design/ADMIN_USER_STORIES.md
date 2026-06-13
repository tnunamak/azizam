# Admin user stories — the operator job map

The admin plane's job is to let an operator run a dynamic, multi-engine
gateway *without editing files or restarting by hand*. This maps every job
an operator comes to do, with the HONEST current status traced against the
code (not aspirational). It is the requirements spec for "nailing the admin
UX." Status legend, and the honesty rule that governs it:

- ✅ **Served** — fully self-serve in the UI, takes effect predictably.
- 🟡 **Partial** — the UI accepts the input but it doesn't fully take effect
  (needs a restart, or only works for some service ids, or silently no-ops).
  **Partial is the dangerous state**: the UI implies a capability it doesn't
  deliver. Every 🟡 must EITHER be finished OR made honest (the UI states the
  limitation) — per the honesty law in WEB_UI_STEERING.md.
- 🔴 **Missing** — no UI/API path; operator must edit files or manage
  processes outside the gateway.

Evidence: `SERVICE_HARDCODING_AUDIT.md` (the 74-finding hardcoding map) +
the end-to-end trace of the flagship story (this doc's Epic A).

---

## Epic A — "Bring a new LLM engine and its models online"

The flagship story. An operator has a new engine (vLLM, a remote
OpenAI-compatible endpoint, a second llama.cpp, LM Studio, Ollama) serving
new models and wants apps to route to it. Traced: **~35–40% self-serve.**

| # | Story | Status | Reality (file:line) |
|---|---|---|---|
| A1 | "Add the engine as a service (host, port, key, capabilities)." | ✅ | Persists to admin.db + writes LiteLLM artifact (admin_runtime add_service). |
| A2 | "Map its models (public → upstream) so callers can request them." | 🟡 | Models reach the LiteLLM model_list (litellm_substrate, registry-driven) — but only after a gateway AND litellm-subprocess restart. |
| A3 | "Have it take effect without restarting the gateway." | 🔴 | No in-process hot reload (admin_runtime:711-713). `LLM_BACKEND_HOSTS`, `TTS/STT/EMBEDDINGS_HOST` are import-time constants. Restart mandatory. **Top gap.** |
| A4 | "Route a chat call for its model to it." | 🟡 | Works via LiteLLM substrate IF the engine is already up + after restart. Native catch-all picks it only as priority fallback when tabby absent (tier-2). |
| A5 | "Have the gateway start/stop/idle the engine for me." | 🔴 | `ensure_target()` returns False for any backend ∉ ("kobold","tabby") (proxy.py:1240). Lifecycle (swap/idle/spawn) is tabby/kobold-hardcoded. Engine must be run externally. |
| A6 | "Use it for a role (e.g. the 'coder' role points here)." | 🔴 | Role-model table silently drops entries whose backend ∉ ("kobold","tabby") (proxy.py:~502). |
| A7 | "See its health in the admin." | ✅ | `check_service_health(id)` is generic (tier-2). Overview/Services reflect it after restart. |
| A8 | "See its process / VRAM on the status page." | 🔴 | `status_endpoint._SERVICE_UNITS` is a hardcoded process-name list. New engine invisible. |
| A9 | "Let it serve embeddings / TTS / STT too (multi-modal engine)." | 🔴 | `/v1/embeddings`,`/v1/audio/*` pinned to single `EMBEDDINGS_HOST`/`TTS_HOST`/`STT_HOST` constants keyed to ids embeddings/tts/stt. A new id can't serve these even if it declares the capability. |
| A10 | "Let it serve vision." | 🟡 | Included in `vision_capable_llm_backends()` (tier-2) after restart; but vision model-swap orchestration is kobold-mmproj hardcoded. |

**Highest-leverage gaps to make A real (in order):**
1. **In-process hot reload** (A3) — an admin action / signal that re-runs
   `_load_effective_runtime_config` and rebuilds the host constants. Single
   most impactful: turns every "save → restart" into "save → live." Until it
   exists, the restart-to-apply banner is the honesty patch.
2. **Generalize `ensure_target`** (A5 partial) — an "externally managed,
   just health-check it" lifecycle so an always-on new engine activates
   instead of 503-ing at the literal gate. Full lifecycle config is Epic E.
3. **Role table accepts any registry backend** (A6) — drop the
   ("kobold","tabby") gate; read from `LLM_BACKEND_HOSTS`.
4. **Capability dispatch tables** (A9) — `/v1/embeddings` & `/v1/audio/*`
   resolve backend from the registry by capability, not a single host const.
5. **Registry-derived status process list** (A8).

---

## Epic B — "Govern who can reach what" (apps, keys, profiles, access)

| # | Story | Status |
|---|---|---|
| B1 | "Create an app and mint/reveal a key once." | ✅ |
| B2 | "Revoke / disable / re-enable an app or key." | ✅ (enable added) |
| B3 | "Define a profile: which models/capabilities/protocols a caller may reach." | ✅ |
| B4 | "Test access — would app X reach model Y? why/why not?" | ✅ (policy simulator surfaced as Test access) |
| B5 | "See per-key last-used, so I can spot stale keys." | 🟡 last_used_at exists; surfaced on key rows but not consistently per-key in the app view (audit SEV-list). |
| B6 | "Change a user's role (grant a teammate admin)." | 🔴 No PATCH /users; roles come from OIDC/provisioning. UI now SAYS so (honesty fix) — but no in-UI grant path for local users. |
| B7 | "Map OIDC groups → profiles for auto-access." | ✅ (groups_claim wired end-to-end) |

---

## Epic C — "Understand and trust what's happening"

| # | Story | Status |
|---|---|---|
| C1 | "What needs my attention right now?" | ✅ Overview attention list. |
| C2 | "Who changed what, when — searchable across the whole log." | ✅ (server-side filter + cursor; honesty fix) |
| C3 | "Is each backend actually up?" | 🟡 Health is probed per-service, but Overview's count says 'enabled' not 'reachable' (honest label); no at-a-glance live reachability roll-up. |
| C4 | "See live request activity / throughput / errors." | 🟡 Firehose + metrics exist (server-rendered), not in the React admin yet. |
| C5 | "Inspect a single request's routing decision (receipts)." | 🟡 Receipts exist; new-engine receipts get no service label (proxy.py:~4587 hardcoded). |

---

## Epic D — "Configure the gateway itself"

| # | Story | Status |
|---|---|---|
| D1 | "Set up sign-in (OIDC) including the groups claim." | ✅ |
| D2 | "Install / enable an extension package." | ✅ add/enable. |
| D3 | "Disable / remove an installed extension." | 🔴 No disable/remove API. |
| D4 | "Control media-job admission (block intents, reserve GPU)." | ✅ |
| D5 | "Set per-service note / role / priority / resources." | ✅ (full edit form, data-loss fixed). |
| D6 | "Edit a non-OpenAI backend honestly (ComfyUI workflow dirs)." | ✅ (protocol-aware form, honesty fix). |

---

## Epic E — "Manage engine lifecycle from the gateway" (the deep gap)

Today entirely hardcoded to tabby/kobold (swap, idle-unload, spawn,
systemd drop-ins). This is the registry's missing `lifecycle` schema and
the reason the gateway "manages records about engines" more than engines.

| # | Story | Status |
|---|---|---|
| E1 | "Declare how an engine is started (external / subprocess / systemd unit)." | 🔴 |
| E2 | "Set idle-timeout / auto-unload per engine." | 🔴 (tabby/kobold only, hardcoded) |
| E3 | "Define model-swap strategy for an engine that hosts one model at a time." | 🔴 (tabby HTTP load/unload + kobold systemd drop-in, both hardcoded) |
| E4 | "Cold-start an engine on first request." | 🔴 (ensure_target literal gate) |

Epic E is genuinely large (per-driver implementations: external, HTTP
load/unload, systemd). It should be designed as its own arc AFTER Epic A's
reload + generalization gaps, which unlock the common "always-on external
engine" case without needing E at all.

---

## Sequencing recommendation

The honest path to "nailed admin UX":

1. **Make Epic A's always-on case fully self-serve** — hot reload (A3),
   generalize ensure_target for externally-managed engines (A5-partial),
   role table (A6), embeddings/audio capability dispatch (A9). This makes
   "add a vLLM/remote endpoint and route to it" a real one-screen job — the
   most common operator intent, and the one the UI currently over-promises.
2. **Close the honest-status gaps** — registry-derived status list (A8),
   reachability roll-up (C3), receipt labels (C5).
3. **Then design Epic E** (managed lifecycle) as its own arc for the
   self-hosted-llama.cpp/tabby case where the gateway runs the process.
4. **Small honesty/coverage cleanups** — extension disable (D3), per-key
   last-used consistency (B5).

Every 🟡 above that we don't finish this pass MUST be made honest in copy
(restart-to-apply banners, "external engine" labels) so the UI never
implies a capability it lacks.

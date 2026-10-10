# Agent instructions — Azizam

This is a self-hosted, OpenAI-compatible gateway that fronts whatever local AI
engines an operator runs (llama.cpp, vLLM, Ollama, TabbyAPI, ComfyUI, TTS/STT,
and whatever replaces them). Read this before doing engine-related work.

**Restart rule:** restart the gateway only with `scripts/safe-restart` (`--llama` for llama-bee), never a bare `systemctl --user restart`. See `docs/ops/SAFE_RESTART.md`.

## THE BINDING PRINCIPLE: track engines as DATA, never as CODE

This is the load-bearing architectural rule of the project. It was arrived at
deliberately; do not relitigate it, and do not erode it by convenience.

**The gateway must not be in the business of tracking inference engines in
code.** Engines churn — assume the ones running today are gone in ~2 years and
replaced, then replaced again. The durable value is engine-**agnostic**: a
stable OpenAI-compatible front door (auth, profiles, routing, honest
observability) over whatever the operator points it at. The stable contracts
(OpenAI/Anthropic/ElevenLabs shape) persist; the engines behind them do not.

**The line:**
- Engine-specific knowledge **as DATA is fine** — seed config rows, presets,
  declarative transforms. Because it is *indistinguishable from what an operator
  could configure themselves*, and it churns harmlessly (add/delete a row). A
  seeded "native" convenience for a known engine is acceptable **only if** it is
  pure data the operator could have authored through the same interface.
- Engine-specific knowledge **as CODE is not** — no per-engine Python class, no
  `if engine == "..."` / `if backend == "..."` branch, no per-engine parser, no
  per-engine remediation string baked into a function. Code is the maintenance
  treadmill: every new engine becomes a PR + release we own. We refuse that
  business.

**The test for ANY piece of engine support:** *"Could an operator have
configured this themselves through the same user-facing interface?"*
- YES → it's data. Express it as a transform/config row, seed it if convenient.
- NO → it's the bad kind. **Cut it**, or — only if genuinely irreducible and
  worth keeping — put it behind the one in-tree code escape hatch
  (`register_builtin` in `gateway/transforms.py`) with a reviewed justification.
  Do **not** build new plugin machinery on speculation; building a plugin system
  is itself the treadmill.

**What this is NOT:** it is not merely "no per-model tool-call parsers" (that was
one symptom). It is "no engine knowledge in core code at all." Detect-and-advise
over parse-and-fix: the gateway observes request/response *shape* generically
(no engine names) and advises the operator to fix their backend, rather than
encoding per-engine fixes it must maintain.

### How engine support is actually delivered

- The fixed, engine-**agnostic** primitive set in `gateway/transforms.py`
  (`value-map`, `drop-params`, `default-params`, `param-map`, `message-inject`,
  `clamp`) is infrastructure, not engine knowledge — it never grows per engine.
- A service record can declare `config["transforms"]: [<id>, ...]` (DATA). The
  dispatch path applies them by id on request and response
  (`_per_backend_transform_ids` in `proxy.py`). A new engine's quirk is a *new
  combination of primitives in config*, never a new class.
- Generic, engine-agnostic spec-repairs (e.g. promoting leaked `<think>` into
  `reasoning_content`, args-stringify, finish_reason repair) are always-on and
  reference no engine names. Fine to keep.
- Exact per-backend remediation, if an operator wants it, is an operator-authored
  data field on the service record — never maintained in code.

### Enforcement (this is checked in CI)

`tests/test_engine_knowledge_boundary.py` fails the build if new per-engine code
appears in core (`if engine ==` / `if backend ==` branches with engine literals,
`*<Engine>*Transform` classes, engine CLI flag literals like `--tool-call-parser`
/ `--jinja`). proxy.py has no allowlist entries: the remaining reviewed engine
literals (the kobold/tabby local-swap lifecycle set and similar properties) live
as data in `gateway/engine_quarantine.py`, which is reviewed as a unit and not
scanned. The former engine-named transform classes are now generic. D-2's engine
framing is removed and D-3 is complete. D-1's generic reasoning transform still
applies automatically to resolved local chat, once behind a resolved-target gate.
**If your change trips this guard:
the fix is to move the knowledge to data, not to add an allowlist entry.** Adding
an allowlist entry requires an explicit, reviewed justification and is the
exception, never the reflex.

The full design and the four-bucket classification of every engine-touching
item live in `docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md`. Read it before adding
or migrating anything engine-specific.

## Working norms for this repo

- Python is always `./venv/bin/python` (bare `python` lacks fastapi). Full suite:
  `./venv/bin/python -m pytest -q`. Keep it green; behavior-preserving migrations
  must prove byte-identical output for the current config.
- The gateway runs live on port 5000 (systemd user service `azizam.service`)
  with the operator's machine-specific paths in a gitignored `.env.local`
  (sourced by `start.sh`). Never touch ports 5000/15000. Never print secret
  values (`opk_`/`oak_`/provider keys). Commit, but push only when asked.
- The service registry (admin.db) is the source of truth for backends; config is
  derived from it at boot, with a hot-reload path so service edits apply live.
- More repo context: `docs/design/` (ENGINE_REGISTRY_DESIGN, ENGINE_TRANSFORMS_DESIGN,
  ADMIN_USER_STORIES, the boundary doc), `CONFIG.md` (env vars), `README.md`.

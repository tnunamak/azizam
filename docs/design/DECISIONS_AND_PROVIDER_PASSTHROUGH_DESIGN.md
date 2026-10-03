# Decisions API and remote provider passthrough

Status: design, not built. Date: 2026-10-03.
Scope: (1) a decisions API with TypeSafe and OpenAI facades, a local backend and
vendor passthrough; (2) a general way to reach paid remote providers (OpenRouter,
OpenAI, Anthropic, TypeSafe) by user choice and owner policy.

Inputs: `docs/LITELLM_SUBSTRATE_DECISION.md`, `docs/VENDOR_FACADES.md` (section 6),
`docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md`, `docs/CONFIG_SOURCE_OF_TRUTH.md`,
`gateway/stable_aliases.py`, `gateway/policy.py`, `gateway/receipts.py`,
`gateway/litellm_substrate.py`, local CPU measurements (section 2.2) and
vendor docs (cited inline).

## 0. Decisions at a glance

| # | Decision | Main trade-off |
|---|---|---|
| D1 | The TypeSafe wire format (`POST /v1/systemone`) is the gateway's one decisions contract. No gateway-native decisions shape. | Ties the internal contract to one vendor's format. Accepted: three independent servers already speak it (TypeSafe, OpenRouter, `decider-ai`). |
| D2 | Mount at `/typesafe/v1/*` (the SDK base URL) and `/v1/systemone` (raw HTTP). | The `/typesafe` prefix avoids a clash with the OpenAI `GET /v1/models` shape. |
| D3 | OpenAI Decisions: reserve `/v1/decisions`. Ship opaque passthrough only when the owner's key has access. Add a local translator only after OpenAI publishes a schema. | No local backend for OpenAI-shaped clients until the schema is public. |
| D4 | Local default: **decider-2b v11 Q8_0 GGUF on CPU** in an on-demand engine that speaks `/v1/systemone`. It uses the same lifecycle as `rerank-cpu`. | It takes 2.5–3 s for a 600–800-token state on this busy CPU, against Jev's 70–500 ms. Short requests take about 0.2 s. A 1.5–2 GB GPU slice would bring the long case to about 0.1 s (estimate). |
| D5 | The model prompt and the logit readout live in the engine, not in gateway code. | One more small engine process. It follows the engine-knowledge boundary. |
| D6 | Remote selection uses provider-qualified names (`typesafe/jev-latest`, `openrouter/anthropic/claude-…`), and stable aliases can point at them. | Model names get longer. The provider is explicit, so it is never a surprise. |
| D7 | LLM-shaped remotes (chat, embeddings, Anthropic Messages) run through LiteLLM. Non-LLM remotes (systemone, decisions) use a small gateway HTTP forwarder. | Two execution paths. LiteLLM does not model decisions. |
| D8 | One control plane in the gateway: Profiles decide who may spend money, and receipts carry the cost. LiteLLM stays a replaceable execution engine. | This argues against the *target* state in the LiteLLM doc (section 5). |
| D9 | No silent fallback from free to paid, ever. Paid fallback needs an explicit alias entry **and** Profile permission, and it is recorded in the receipt. | Some requests fail that a paid fallback could have answered. |
| D10 | Provider keys are secret references (an env name today). Never put values in YAML, admin.db, receipts or logs. A vendor 401 never reaches the client as a 401. | No BYOK by request header in v1. |

## 1. What exists today (verified in code)

- **Facades** are thin adapters on the core (`VENDOR_FACADES.md` §6). Examples:
  `register_a1111_routes(app, deps)` (`gateway/a1111_api.py:317`) and frontend
  records in `registry/protocol_frontends.yml`. Every facade must be mounted
  before the catch-all (`proxy.py:26622`).
- **Stable aliases** (`gateway/stable_aliases.py`) are per-operation sections in
  `proxy_config.yml`. Each section has `stable`, `openai` glob patterns and
  `engines`. A stable alias that names a list of engines resolves to the first
  available engine and keeps the rest as `fallbacks`. The operations are chat,
  embeddings, images, image_edits, videos, speech, transcriptions,
  audio_generate and rerank. A `decisions` operation does not exist yet.
- **Profiles** (`gateway/policy.py:144`): `AppProfile` already has
  `fallback.cloud_allowed` (default False, seeded False in every default
  profile). Nothing routes to a cloud backend today. No profile has a money
  budget.
- **Receipts** (`gateway/receipts.py`) store cost only as admin-visible
  `metadata.litellm.response_cost`. They have no usage or provider fields.
- **Secrets**: `start.sh` generates the LiteLLM keys. The systemd unit sets plain
  `Environment=` lines. Registry records keep `api_key_env` or `secret_refs`, and
  inline values become `<external-secret>`
  (`gateway/service_registry.py:1426`). The LiteLLM compiler emits
  `api_key: os.environ/NAME` (`gateway/litellm_substrate.py:366`). admin.db
  never stores provider secrets (`gateway/admin_store.py:19`).
- **LiteLLM** is pinned exactly in `requirements.txt` (`litellm[proxy]==1.86.2`,
  with a comment about the compromised 1.82.7/1.82.8). It is not
  hash-locked.
- **On-demand CPU engine pattern**: `rerank-cpu.service` runs `llama-server`
  with `CUDA_VISIBLE_DEVICES=` and `MemoryMax=5G`. The gateway starts it on first
  use (`driver: systemd`) and it exits after 600 s idle
  (`scripts/rerank-idle-exit.sh`).

## 2. Evidence

### 2.1 Wire formats (checked 2026-10-03)

**TypeSafe** (https://docs.typesafe.ai/api.md, https://docs.typesafe.ai/models.md):
- Request: `POST /v1/systemone`, `Authorization: Bearer`, body `{model, state, questions: {id: Question}}`.
  - `state` is a string, object or array. Question ids are never shown to the model.
  - `noul`: `instructions`, optional `criteria {true, false}`.
  - `choice`: `criteria {option: description|null}`, 2–255 options.
  - `score`: `criteria [level descriptions]`, 2–10 levels.
- Response: `{model: "jev-1.13.0", answers: {id: Answer}, usage: {input_tokens, output_tokens}}`.
  - A choice answer has `choice`, `probabilities` and `confidence`.
  - A score answer has `score` (the expected level), `legend`, `probabilities` and `confidence`.
  - A noul answer has `noul` (P(yes)).
- Errors: 401, 422, 429 and 529. The SDK retries 429 and 529 with backoff.
- `GET /v1/models` lists aliases. `jev-latest` and `jev-preview` currently both point to `jev-1.13.0`.
- Price: $0.042 per M input tokens; output is free.
- Limits: 80 req/s and 100K tok/s, "adjusting dynamically". Context is 64k per request, and 32k for the state plus the longest question.
- No streaming and no batch endpoint.
- The Python SDK `typesafe-sdk` reads `TYPESAFE_BASE_URL` (the API root).

**OpenRouter** serves Jev with the same shape at `POST /api/v1/systemone`, and
also at `POST /api/alpha/decisions`. It returns `usage.cost` in USD
(https://openrouter.ai/docs/guides/community/jev).

**decider-ai** (Apache-2.0, https://huggingface.co/Mapika/decider-2b) offers
`Decider.system_one()` and `decider.serve` with `POST /v1/systemone` "in
TypeSafe's format; the official `typesafe-sdk` works against it unchanged".
Its readout is the softmax over the option-letter logits at each answer slot,
divided by a temperature fitted per answer type (choice 1.164, noul 1.624,
score 1.124 for 2b-v11). It adds `x_p_max`, `certainty`, `level_fit` and
`fit_mass`. Its `confidence` follows TypeSafe's definition.

**OpenAI Decisions**: announced at DevDay on 2026-09-29, limited preview, GPT-6
Luna, 150 ms on the slide.
- `POST https://api.openai.com/v1/decisions` exists. A standard key gets
  `403 "Decision API is not enabled for this user."`, even with an empty body
  (https://www.eesel.ai/blog/openai-decisions-api).
- No guide, API reference, OpenAPI path, SDK method or price was found through
  openai-python v3.24.0 (2026-10-02).
- Circulating `decisions.create({input, schema})` snippets are guesses
  (https://github.com/thejustinwalsh/yamadori/issues/7).
- Not re-verified today beyond those sources.

### 2.2 Local tiny model: measured on this box (CPU only, no GPU touched)

Setup:
- Ryzen 9 7900X (12C/24T, AVX-512). The box was busy: load average 11–21
  during every run (postgres, Claude and the speech services).
- Files: `decider-2b-v11-Q8_0.gguf` (2.0 GB) and `decider-4b-v2.1-Q4_K_M.gguf`
  (2.7 GB), from `Mapika/decider-*-GGUF`, in
  `/media/windows/AI Models/LLMs/GGUF/decider-*/`.
- Runtimes: `decider-ai` 1.8.1 with llama-cpp-python 0.3.36 (CPU build), and
  the stock `llama-server` b11337, run on ports 18931–18932 and stopped
  afterwards. 8 threads, `n_ctx` 4096, `CUDA_VISIBLE_DEVICES=""`.
- Scenario: a Daisy agent state of 461 tokens. Requests take 596–940 input tokens.

| Request | decider-2b Q8_0 | decider-4b Q4_K_M |
|---|---|---|
| RSS after load (in-process, includes 0.33 GB torch import) | 2.6 GB (peak 2.7) | 5.0 GB (peak 5.1; Q4_K CPU repack) |
| RSS, `llama-server` only | 2.4 GB | not run |
| Cold load | 5–11 s | 4.5 s |
| Raw prefill / generation (llama-bench pp600 / tg32) | 255 / 21 tok/s | 151 / 15 tok/s |
| 12 basic one-question decisions (≤40-token states): accuracy, median latency | 11/12, **220 ms** (max 288) | 11/12, 440 ms (max 655) |
| 1 choice, 12 described options, 696 tokens | 2.8 s | 5.3 s |
| 1 choice, 20 options, 596 tokens | 2.5 s | 4.1 s |
| 4 mixed questions (choice + 2 noul + 5-level score), independent rows (default) | 16.4 s | 30.1 s |
| same, packed into one row (`independent=False`) | 2.9 s | 6.1 s |
| same, independent rows, `llama-server` with the shared state primed once (`-cms 0`) | **4.3 s** first, 2.8 s with the same state again | not run |

Findings:
1. **Single-pass option scoring works on a stock `llama-server`.** For each
   row, send the prompt token ids up to the answer slot with `n_predict: 1`,
   `n_probs: 100` and `post_sampling_probs: false`. Then softmax the letter
   log-probs divided by the type temperature.
   - Against decider-ai's in-process readout, the maximum absolute probability
     difference was 0.0036 over all answers, with the same argmax.
   - With prefix priming, the maximum difference was 0.018 (checkpoint restore),
     again with the same argmax.
   - So no new inference runtime is needed. The model-specific part is only
     prompt building and readout.
2. **Scoring and generation compared.** Scoring reads all options from one
   prefill, with no decode steps. Generating the same answers as JSON (about
   20–40 tokens) would add about 1–2 s at 21 tok/s on top of the same prefill.
   It would also lose the calibrated distribution.
3. **The CPU prefill of about 250 tok/s is the floor.** Latency is about
   (state + question tokens) / 250 s.
   - Prefix reuse is the lever for many questions over one state. decider-ai's
     GGUF engine has no prefix cache: "score_shared has no prefix cache here".
   - Priming `llama-server` with the common prefix cut the mixed request from
     12.2 s to 4.3 s.
4. **Quality.** On the one hard agent-routing request:
   - The 4B chose `bg_run_video` (0.88) and flagged the policy violation (0.97).
     Both are right.
   - The 2B chose `job_status` (0.45) and missed the violation (0.27). Its low
     confidence shows it was unsure.
   - This is one example, not a benchmark. It agrees with the Jev Decision Index
     frozen scores: 2B 18.9, 4B 27.5, Jev 43.6, best 26–28B open model 39.8.
   - Both models handled basic routing, sentiment and yes/no equally well.
5. **GPU slice (estimate, not measured; no VRAM taken).**
   - decider-2b Q4_K_M is a 1.3 GB file and about 1.5 GB of VRAM with a 4k
     context. Q8_0 needs about 2.3 GB.
   - On a 3090, llama.cpp prefill for a 2B model is in the thousands of tok/s.
     A 600–800-token decision would then take about 0.1–0.2 s.
   - The board measured 8 ms for decider-2b with FP8 and CUDA graphs on an
     RTX PRO 6000.
   - A GPU slice is therefore the change that makes it "Jev-fast". CPU is "usable for basic tasks".

**Recommendation: decider-2b v11 Q8_0 is the default local backend.**
- It has the best quality per GB and per ms among small models with an official
  GGUF and a calibrated readout. Its ECE is 0.077 on the board.
- Q4_K_M (1.3 GB) loses only 0.3–0.5 points, but its error grows on long states
  (model card).
- decider-4b Q4_K_M is the opt-in "local quality" tier: twice the RAM and latency.
- Rejected:
  - GLiNER and encoder entrants: frozen scores of 1–6. Too weak.
  - The 0.8B models (JPT-0.8B 13.7, decider-0.8b): an option if RAM matters
    more than quality. Not measured.

Calibration and prompt mapping:
- decider-2b was trained on and calibrated for the TypeSafe question types:
  noul, choice up to 255 options, score 2–10 levels with isolated level rows.
  Its config declares `max_options: 255` and `max_state_tokens: 32768`.
- So a TypeSafe request maps 1:1 with no gateway translation.
- For OpenAI Decisions, the mapping is unknown until the schema exists.
  "Finite predefined answers" would map to `choice`.

### 2.3 Prior art: passthrough and user choice

| Product | How a client picks | Keys | Cost per request | Paid fallback |
|---|---|---|---|---|
| LiteLLM proxy | `model` = `provider/model` or an alias; `pass_through_endpoints` for other APIs | `os.environ/VAR` (OSS); secret managers need Enterprise (https://docs.litellm.ai/docs/secret) | `x-litellm-response-cost` header | Opt-in `router_settings.fallbacks`, budget re-checked (https://docs.litellm.ai/docs/proxy/reliability) |
| OpenRouter | `model` slug plus a `provider` object (`order`, `only`, `ignore`, `max_price`, `data_collection`, `sort`) and `:nitro`/`:floor` suffixes (https://openrouter.ai/docs/guides/routing/provider-selection) | Account credits or BYOK | `usage.cost` in every response | **On by default** (`allow_fallbacks: true`) |
| Portkey | Config `strategy.mode` = fallback, loadbalance or conditional, with targets (https://portkey.ai/docs/product/ai-gateway/fallbacks) | Virtual keys | Logged per trace | Opt-in by config |
| Cloudflare AI Gateway | Provider path passthrough in native format (https://developers.cloudflare.com/ai-gateway/usage/providers/) | Request key, else stored BYOK, else Unified Billing credits | Analytics | Requests can land on paid credits without notice (https://developers.cloudflare.com/ai-gateway/features/unified-billing/) |

Lesson: copy LiteLLM's and OpenRouter's `provider/model` naming and OpenRouter's
per-response `usage.cost`. Do **not** copy OpenRouter's default-on fallback or
Cloudflare's silent move to billed credits. Those are what D9 forbids. LiteLLM's
auto-router can already call Jev as a classifier
(https://docs.litellm.ai/docs/proxy/auto_routing). That is a routing feature and
not a decisions endpoint, so it does not change D7.

## 3. API surfaces

### 3.1 TypeSafe facade (canonical)

- `POST /typesafe/v1/systemone` and `GET /typesafe/v1/models` give TypeSafe SDK users
  `TYPESAFE_BASE_URL=https://<gateway>/typesafe` with their gateway App key as
  `TYPESAFE_API_KEY`.
- `POST /v1/systemone` is the same handler at host root, for raw HTTP clients.
  It does not clash with anything.
- `GET /typesafe/v1/models` returns the decision models this key's Profile can use,
  in TypeSafe's list shape. Root `GET /v1/models` is OpenAI-shaped and stays so.
  This is why the SDK base URL carries a prefix.
- The handler validates locally before any backend call. It checks types, 2–255
  options, 2–10 levels, a non-empty instructions or noul criteria, and token
  limits per backend. A failure returns TypeSafe-shaped `422`. No paid call is
  made for a bad request.
- The response is the backend's body unchanged, except that `model` reports what
  really answered (`decider-2b-v11`, `jev-1.13.0`). TypeSafe does the same.
  Extra fields from decider (`x_p_max`, `certainty`, `level_fit`) pass through.
  TypeSafe clients ignore unknown fields.
- Gateway headers: `x-gateway-request-id`, `x-gateway-backend`, `x-gateway-remote: 0|1`,
  `x-gateway-cost-usd`.

### 3.2 OpenAI Decisions facade (schema unknown)

- Reserve `POST /v1/decisions` in `registry/protocol_frontends.yml`.
- **Until a schema exists:**
  - If a Profile has `openai` in its remote providers and the owner configured
    `OPENAI_API_KEY`, forward the body as-is to `api.openai.com/v1/decisions`.
  - Return OpenAI's status and body, including its 403 "not enabled". The
    receipt records the cost as `unknown` with `source: none`, and the response
    gets `x-gateway-cost-usd: unknown`.
  - Otherwise return OpenAI-shaped `404 model_not_found` with
    `code: decisions_backend_unavailable`.
- **Staying ready:** a weekly CI job diffs `openai/openai-openapi` and the
  openai-python changelog for `decisions`. When a schema lands:
  1. Pin a trimmed copy in `tests/fixtures/` (sha256 and fetch date).
  2. Write a pure translator in `gateway/protocols/openai_decisions.py` to and
     from the systemone core shape.
  3. Run the official SDK fidelity test.
  4. Then local and TypeSafe backends serve OpenAI-shaped clients too.
- Do not guess the schema before then (`VENDOR_FACADES.md`: never accept and
  ignore output-changing parameters).

### 3.3 No native gateway shape

No `/gateway/v1/decisions`. Nothing justifies it: the TypeSafe shape already has
typed questions, probabilities, multi-question requests and a model field. The
gateway adds its value through headers, receipts and discovery, not a new body.
Revisit only if OpenAI's schema needs something the core shape cannot express.

## 4. Backends and routing

All decision backends speak `/v1/systemone`. The gateway is a router and policy
point over the backends. It does not hold model knowledge.

| Backend | How | Cost class | Default |
|---|---|---|---|
| `decider-2b` local | On-demand CPU engine unit `decider-cpu.service` (`MemoryMax=4G`, idle exit 600 s), `driver: systemd` | free-local | **yes**, the `decide` alias |
| `decider-4b` local | Same unit pattern, second model file | free-local | opt-in alias `decide-quality` |
| llama-bee 27B logprob | Engine-side adapter: systemone prompt to `/completion` with `n_probs` on llama-bee (the board's "simple-jev" Qwen3.8-27B scored 37.0, 85% of Jev) | free-local, **contends with chat** | opt-in alias `decide-27b`; not measured; must queue behind chat and never swap the model |
| TypeSafe | Gateway HTTP forwarder to `https://api.typesafe.ai/v1/systemone`, owner key | paid-remote | only by explicit name or an owner-set alias |
| OpenRouter Jev | Forwarder to `https://openrouter.ai/api/v1/systemone` | paid-remote | as above |
| OpenAI Decisions | Opaque forwarder (section 3.2) | paid-remote, cost unknown | as above |

The local engine has two options. The choice belongs to the ops slice, and the
gateway cannot tell them apart:
- **E1 (recommended): a stock `llama-server` plus a ~150-line readout wrapper.**
  The wrapper builds prompts with `decider-ai`'s pure-Python prompt code, primes
  the shared state prefix, and serves `/v1/systemone`. It reuses the measured
  runtime and its prompt cache, and needs no torch. It ran within 0.004 of the
  reference.
- **E2: `decider-ai` `Decider(gguf)` in-process.** This is 30 lines today, but
  it imports torch (+0.33 GB) and has no prefix cache. `decider.serve` does not
  serve GGUF.
- **Upstream first:** propose GGUF serving, a torch-free GGUF path and prefix
  priming to `Mapika/decider`. If they land, E1 shrinks to `decider.serve`.

### 4.1 Aliases and user choice

Add an operation `decisions` to `model_aliases`:

```yaml
model_aliases:
  decisions:
    engines:
      decider-2b:  {service: decider-cpu,  model: decider-2b-v11, placement: "CPU, 2.4 GB RAM while running, on demand"}
      decider-4b:  {service: decider-cpu-4b, model: decider-4b-v2.1, placement: "CPU, 5 GB RAM while running, on demand"}
    stable:
      decide: [decider-2b]                # default, free
      decide-quality: [decider-4b, decider-2b]
    openai:
      - {match: "jev-*", to: decide}      # TypeSafe SDK default model works out of the box, locally, for free
      - {match: "*/*", passthrough: true} # provider-qualified names go to the provider resolver
```

- **Remote names are always provider-qualified:** `typesafe/jev-latest`,
  `openrouter/typesafe/jev-1.13` and `openai/<decisions model>`. The same
  grammar covers chat: `openrouter/anthropic/claude-sonnet-…`, `anthropic/claude-…`.
- The owner may repoint `jev-*` to `typesafe/jev-latest` with one config line.
  That is an explicit owner choice, shown in discovery and on every receipt.
- **Unqualified names never resolve to a paid provider unless an alias says
  so.** This keeps today's precedent: `gpt-*` and `claude-*` already resolve to
  local aliases.
- Per request, the user chooses with `model`. There is no hidden header that
  overrides it.

## 5. The LiteLLM boundary

The coordinator's position, tested claim by claim:

| Claim | Verdict | Evidence |
|---|---|---|
| LiteLLM should be the adapter substrate for LLM-shaped remotes | **Agree** | The current boundary already says this. `litellm_substrate.py` can already emit non-loopback `api_base` with `os.environ/` keys. Writing our own adapters would break "do not build a second … provider adapter system". |
| SSO beyond 5 users, JWT/OIDC, key rotation, secret managers, tag budgets and per-model key budgets are Enterprise | **Confirmed** | https://docs.litellm.ai/docs/enterprise and https://docs.litellm.ai/docs/secret. The repo LICENSE is MIT except `enterprise/`. Not confirmed: whether audit logs and team model access are Enterprise. |
| Virtual keys and budgets need Postgres | **Confirmed** | https://docs.litellm.ai/docs/proxy/virtual_keys |
| PyPI 1.82.7 and 1.82.8 were backdoored on 2026-03-24 (TeamPCP) | **Confirmed by security vendors** | Wiz, Datadog Security Labs, JFrog. No BerriAI advisory found. Our pin is exact but **not hash-locked**. |
| Media, local resources and decisions need gateway policy anyway, so the target state means two policy engines | **Agree, and it decides the question for paid remotes** | Decisions and media never touch LiteLLM, so the gateway must gate their spend. A second budget engine for LLM calls would split one owner's money across two ledgers. |
| Virtual keys bypass Profile policy | **True today, and unacceptable for paid models** | `LITELLM_SUBSTRATE_DECISION.md` "It bypasses gateway Profile policy for those calls". |

Resulting boundary for this work:
- **LiteLLM owns:** provider adapters, schema translation, streaming, retries
  within one provider, and the cost table (`x-litellm-response-cost`).
- **The gateway owns:**
  - whether a caller may reach a paid provider at all;
  - the budget;
  - provider-key custody (as env references);
  - receipts;
  - all decisions traffic.
- Paid remote model groups are compiled with `model_info.gateway_cost_class: paid-remote`.
  The gateway refuses them for any caller whose Profile lacks permission,
  **including virtual-key callers**. The bypass stays only for free-local
  groups, until the open migration question is settled.
- LiteLLM `pass_through_endpoints` for TypeSafe or OpenRouter systemone is
  rejected. It adds a hop and a master-key auth model, and it gains nothing.
  LiteLLM has also logged Jev spend as 0 (https://github.com/BerriAI/litellm/issues/42200).

**Disagreement to record:** this design contradicts the doc's *target* state,
where LiteLLM owns budgets and spend. That target is marked "directional, not
yet adopted". Recommendation: amend it so that money policy (who may spend and
how much) stays in the gateway, while LiteLLM keeps metering as an input. Also
add hash-locking (`uv pip compile --generate-hashes`) to the LiteLLM pin.

**Prior sessions, briefly** (session `1d254b40`, June 2026):
- Tim asked to "maximally leverage LiteLLM's power and minimally duplicate"
  it, and stressed "we are not building this for me as the only user".
- The assistant moved between sidecar, library, "Option F" (a gateway spend gate,
  with LiteLLM only for `completion_cost`), and "LiteLLM as substrate". Each move
  followed pushback, not new evidence.
- Weak points:
  - LiteLLM facts were stated from memory and were wrong twice ("only does chat").
  - "Budgets are advisory-only" was never verified.
  - Cloud providers, key storage, pass-through and fallback were never designed,
    although the session called cloud fan-out "the single thing that decides this".
- The waspflow lane then built virtual-key admin, which is the opposite of
  Option F's "no Postgres or virtual keys".
- This design closes the cloud gap with checked facts, not memory.

## 6. Provider keys and secrets

- A provider is a registry service record (`kind: provider`). Its fields:
  - `id`
  - `base_url`
  - `protocols`: `openai-chat`, `anthropic-messages`, `typesafe-systemone`, `openai-decisions`
  - `api_key_env` (for example `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`)
  - `cost_class: paid-remote`
  - `price` data, where the vendor publishes it: TypeSafe input $0.042/M
  - `data_policy` note: for example OpenRouter `data_collection: deny` passed through
- The **value** lives only in the secret/bootstrap plane (`CONFIG_SOURCE_OF_TRUTH.md`
  plane 4). That is a systemd `EnvironmentFile` (mode 0600) or `LoadCredential=`,
  filled from Infisical by the existing ops path. Never YAML, admin.db or
  provisioning bundles.
- The admin UI and discovery show `configured | missing`, never the value.
  A provider whose key is missing appears in discovery as `available: false`
  with `reason: provider_key_missing`.
- The forwarder builds the vendor `Authorization` header from the secret ref. It
  never forwards the client's gateway key, and never forwards a client-supplied
  vendor key (no BYOK in v1).
- Redaction tests assert that no secret value appears in receipts, logs, error
  bodies or `/admin/api` output.

## 7. Policy, profiles, cost in receipts

Profile addition (stored in admin.db as Profile JSON, edited in `/admin`):

```json
"remote": {
  "providers": ["typesafe", "openrouter"],
  "max_usd_per_request": 0.01,
  "budget_usd_per_day": 1.00,
  "allow_paid_fallback": false
}
```

- The default is `remote` absent, so no paid provider is reachable. All seeded
  profiles stay local-only.
- `fallback.cloud_allowed` keeps its meaning (automatic fallback). It is
  separate from explicit selection, and both default to false.
- **Pre-call check:** estimate the cost from the price data and the input
  tokens. For TypeSafe, that is the characters/4 bound × $0.042/M. Reject over
  `max_usd_per_request` with `402`-style `budget_exceeded` before any spend.
- **Daily budget:** the sum of `cost.usd` in `receipt_store` for the Profile
  over the last 24 h, checked before the call. One SQLite query on an indexed column.
- **Receipt fields (new):**
  - `cost {usd, source: provider-reported|litellm|price-table|unknown, provider, cost_class}`
  - `usage {input_tokens, output_tokens}`
  - `remote: bool`
  - These are **client-visible for the caller's own requests**: users who choose
    remote should see what it cost. Provider names and key refs stay admin-only.
- **Sources:** OpenRouter `usage.cost`, LiteLLM `x-litellm-response-cost`, and
  for TypeSafe the price table × `usage.input_tokens`.

## 8. Capabilities discovery

- New intent `decision.systemone`, one `Capability` per decision model the
  caller may use. Fields:
  - `model`, `backend_kind: local|remote`, `cost_class`
  - `limits {max_options: 255, max_levels: 10, max_state_tokens}`
  - `latency_class` (`cpu-seconds`, `gpu-sub-second`, `remote-sub-second`)
  - `calibrated: true`
  - `available` plus `reason`
- Remote chat models get the same `cost_class` and `remote` fields.
- List `/typesafe/v1/*`, `/v1/systemone` and `/v1/decisions` in `/.well-known/ai-gateway`
  and `llms.txt` with their support tier (`docs/API_TARGETS.md`).

## 9. Failure and fallback semantics

| Situation | Response | Fallback |
|---|---|---|
| Invalid request | TypeSafe `422`, nothing called | none |
| Local engine starting (cold load 5–11 s) | Wait up to the request deadline (default 15 s). Then `529 overloaded` with `retry-after`, which the TypeSafe SDK retries | the next **free** engine in the alias list only |
| Local engine busy (one slot; queue > 4) | `529` with `retry-after` | same |
| Remote 429/529/5xx | Pass the status and body through; no retry inside the gateway (the SDK already retries) | **none to paid**; to a free engine only if the alias lists it |
| Remote 401/403 (owner key bad or not enabled) | `502 provider_auth_failed` (a 401 would tell the client *its* key is bad) | none |
| Provider key missing, or Profile lacks the provider | `403 remote_provider_not_allowed`, before any call | none |
| Over budget | `402`-style `budget_exceeded` | none |

Rule D9:
- An alias may list a paid engine after a free one only when
  `allow_paid_fallback: true` is in the caller's Profile.
- Every fallback writes `fallback_reasons` and the final `cost` to the receipt.
- Free-to-free fallback (2B behind 4B) is allowed by default.

## 10. Streaming and latency budgets

- systemone and the visible OpenAI shape do not stream. The decisions facade
  returns one JSON body.
- Remote chat streaming already passes through LiteLLM. Cost arrives at the end
  of the stream and is written to the receipt after completion.
- Budgets:
  - Gateway overhead: < 10 ms p50, covering auth, policy, validation and receipt write.
  - Local 2B on CPU: 0.2–0.3 s for short states, about 2.5–3 s at 600–800 tokens,
    and about 4.3 s for 4 questions over one state with prefix priming.
  - GPU slice (estimate): 0.1–0.2 s.
  - TypeSafe: 70–500 ms claimed (the board measured a 524 ms median round trip).
  - OpenRouter Jev: 0.21 s p50 (third-party figure).
- Discovery advertises `latency_class` so clients choose knowingly.
- Packed rows (`independent=False`) are 5x faster on CPU for many questions, but
  answers then depend on the other questions. Expose this only as an engine
  option, not a gateway parameter. TypeSafe has no such knob.

## 11. Tests

- **Facade fidelity:**
  - The official `typesafe-sdk` against a hermetic gateway with a fake backend.
  - `system_one()` with all three types.
  - `models.list()`.
  - 422, 429 and 529 behavior.
  - Validation against a pinned trimmed copy of `api.md` (sha256 and fetch date).
- **Engine parity** (ops slice): readout through `llama-server` matches
  `decider-ai` in-process within 0.02 with the same argmax, using the scenarios
  in this doc. This was measured: 0.0036, and 0.018 with priming.
- **Policy:**
  - A paid provider is denied without `remote.providers`.
  - A virtual key cannot reach `paid-remote` groups.
  - No fallback from free to paid.
  - Budget exhaustion.
  - A missing key gives `available: false`.
- **Receipts:** `cost` is present and correctly sourced for each backend. The
  caller sees its own cost and no provider key refs.
- **Secrets:** a grep test over receipts, logs and admin JSON finds no secret
  value. The compiled LiteLLM YAML holds only `os.environ/` refs.
- **OpenAI Decisions:** passthrough relays the 403 unchanged. CI watches for a
  schema.
- **Supply chain:** requirements are hash-locked, and CI fails on an unhashed LiteLLM.

## 12. Build plan (small slices; each one ships and is tested alone)

1. **Facade core:** `/typesafe/v1/systemone`, `/v1/systemone`,
   `/typesafe/v1/models`, validation, the `decisions` alias operation, receipts
   and SDK fidelity tests against a fake engine. No real backend yet.
2. **Local engine (ops):** the `decider-cpu.service` on-demand unit and the
   readout wrapper (E1). Wire the `decide` alias and the parity test. Write an
   `inbox/` request for the unit; nothing touches live units without Tim.
3. **Provider records and secrets:** `kind: provider`, `api_key_env`,
   `configured|missing` in admin and discovery, and the redaction tests.
4. **Profile `remote` block and cost:** pre-call estimate, daily budget and
   client-visible `cost` in receipts.
5. **TypeSafe and OpenRouter systemone forwarders** behind slices 3–4.
6. **Remote LLMs through LiteLLM:** OpenRouter first. Compile `paid-remote`
   groups and close the virtual-key bypass for them.
7. **OpenAI Decisions opaque passthrough** and the schema watch. Add the
   translator once a schema exists.
8. **Opt-in local tiers:** `decide-quality` (4B) and `decide-27b` (llama-bee
   logprob adapter, measured first without disturbing chat).
9. **GPU slice option:** only on Tim's go, with a VRAM placement decision.

## 13. Open questions for Tim

1. **GPU slice:** is about 1.5 GB on GPU0 (2B Q4_K_M) acceptable, to move local
   decisions from seconds to about 0.1 s? Or does CPU stay the default?
2. **`jev-*` default:** should it map to local (free, precedent) or to real
   TypeSafe when a key exists?
3. **Cost visibility:** should cost be visible to every caller for its own
   requests (proposed), or to the owner only?
4. **BYOK:** should a client ever send its own vendor key? Proposed: no in v1.
5. **LiteLLM target state:** may this design's "money policy stays in the
   gateway" amend `LITELLM_SUBSTRATE_DECISION.md`, and may virtual keys lose
   access to paid groups?
6. **Which remote providers first:** OpenRouter alone covers OpenAI, Anthropic and
   Jev with one key and per-response cost. Direct OpenAI and Anthropic keys
   matter only for features OpenRouter lacks, such as OpenAI Decisions preview access.

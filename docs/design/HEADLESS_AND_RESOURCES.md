# Headless surfaces and resource visibility (design v1)

Status: proposed design, not implemented. 2026-10-11.

Evidence (copies in divan `local-ai-gateway/`, filed with this version):
- Resource observability survey: Ollama, vLLM, SGLang, TGI, llama.cpp,
  LM Studio, LocalAI, ComfyUI, LiteLLM, OpenRouter, NVML/DCGM, Kubernetes,
  Kueue, Slurm, Ray, Modal, RunPod, Replicate, Cloudflare, Home Assistant,
  Plex, Jellyfin.
- Headless surfaces survey: Stripe, GitHub, Tailscale, Supabase, Vercel,
  Cloudflare, Linear, Home Assistant, Kubernetes, Ollama, MCP specification.
- Two commit-pinned audits at `9b515251`: UI/API/MCP parity, and resource
  facts the code already has.
- Earlier divan entries on GPU contention on a shared desktop GPU.

## Problem

Two owner requests share one root cause:

1. People cannot tell why a request is slow or refused, what is using the
   GPU, or what fits now. The owner cannot see who occupies a scarce lane.
2. Many actions exist only in the browser UI. Agents and scripts cannot do
   them, and some rules are implemented twice (profile selection in Home and
   in runtime identity; the 50/200 page size in two routers).

The audits show the gap is mostly exposure, not missing logic. Admission
already computes reasons, free capacity and conflicting locks, then discards
them. The admin API already accepts owner and admin bearer keys. There is no
CLI, and the MCP server has only the three data-plane tools.

## Principles

1. One core, thin adapters. Authentication, validation, authorization,
   admission, budgets, durable jobs, engine lifecycle and receipts live in
   the core. REST, CLI, MCP and UI translate; none decides policy.
2. The contract is the OpenAPI document the server already emits, plus a
   small operation registry for facts OpenAPI cannot carry. No second
   hand-maintained specification.
3. Truth before estimates. Show observed phases and reasons. Show an ETA
   only after calibration data exists, as a range with its age.
4. Separate quantities. Device memory, engine KV-cache use, busy fraction,
   reserved capacity, request timings and billed cost are different numbers.
   No single "GPU usage" figure.
5. Privacy by audience. Members see their own work and an anonymous
   capacity class. Owners see attribution, never prompt content.
6. Unknown is not zero. A missing or stale sensor is a reason for
   conservative admission, never evidence of free memory.

## Decisions

### D1. Operation registry beside the routes

Each operation gets a declaration next to its handler: stable ID, required
permission and resource resolver, effect class (read, inference, lifecycle,
destructive, external transmission, billing or security), job behaviour,
retry and idempotency policy, plan support, audience visibility and
redaction rules. Adapters read the registry; it never contains business
logic. Parity tests iterate it.

### D2. Three API areas, one authorization system

| Area | Examples | Credentials |
|---|---|---|
| Compatibility data plane | OpenAI, Anthropic, Ollama routes | App and person inference keys |
| Native work plane | capabilities, plan, submit, job, receipts, status | Sessions, developer keys, MCP |
| Owner control plane | people, keys, plans, categories, engines, providers, settings | Owner and admin keys only |

A path prefix is organization, not authorization. An inference key can never
mint a stronger key, raise its own budget, enable paid fallback or unload
another person's engine, through any adapter.

### D3. Durable jobs and a side-effect-free plan

Native jobs (the existing media jobs, extended) have an opaque ID and the
states `queued`, `preparing`, `running`, `cancelling`, `succeeded`,
`failed`, `cancelled`. A refusal is a result, not a fake job. Submission is
persisted before expensive work, with an idempotency key scoped per
principal and operation; the same key with different input fails.

`plan` turns the admission explanation into a read-only operation: route,
warm or cold, admission decision, permitted alternatives, cost ceiling,
policy revision, and estimate ranges only when calibrated. A plan never
loads an engine or buys cloud work, and it reserves nothing.

### D4. Request phases and reasons on every receipt

Receipts gain observed timestamps (`accepted_at`, `queued_at`,
`started_at`, `first_output_at`, `finished_at`) with a quality flag
(measured, estimated, declared, unavailable) and structured reason codes
from the admission decision. The caller sees a safe reason and a next
action; the owner sees quantities and the policy rule. Existing admission
codes stay; an audience dictionary translates them.

Reason families: capacity (`insufficient_vram`, `display_headroom_reserved`,
`engine_busy`, `host_memory_pressure`), fairness and policy
(`principal_concurrency_limit`, `app_concurrency_limit`, `budget_exhausted`,
`capability_not_allowed`), lifecycle (`model_loading`, `engine_starting`,
`backend_unavailable`, `telemetry_stale`).

### D5. Owner "Now" and member status

A sampler records per device: total, used and free memory, Azizam's
committed reservations, the display-GPU reserve, external processes where
the driver reports them, and host memory pressure, each with `observed_at`.
About 1-2 s while busy and 5-10 s idle; raw samples kept 24 h, minute
rollups 7 days, hourly 90 days. Engines report residency, device placement,
running and waiting counts, and load duration where the backend exposes it
(engine specifics stay as data in the service record).

- Owner: devices, active engines and jobs with person and app attribution,
  recent refusals and why, what fits now (ready, must load, can queue,
  incompatible, denied).
- Member: own jobs and receipts, and a shared-capacity class ("busy with a
  large job", "available"), no other person's identity or content.
- App key: jobs in its own scope only.

### D6. Surfaces to build first

- REST: document the existing owner bearer routes in OpenAPI; add `plan`,
  `status` (audience-filtered "Now") and phase fields on receipts and jobs.
- CLI: a small curated `azizam` CLI over a client generated from OpenAPI:
  `auth`, `capabilities`, `run`, `jobs list|get|wait|cancel|output`,
  `receipts`, `status`, `plan`, selected owner commands, plus `api` as an
  authenticated raw escape hatch and `schema`. JSON on stdout, NDJSON for
  events, progress on stderr, stable error codes in the payload, never
  prompts when not interactive.
- MCP: keep `capabilities`, `run`, `job`; add `status` and `plan` fields to
  them before adding tools. Owner tools only on an explicit owner
  connection, default-deny, each mapped to a registry operation. Tool
  annotations are hints, not access control.

## Slices (each ships with REST, CLI and MCP parity tests)

1. **Truth from existing facts.** Persist admission reasons, free capacity
   and conflicting locks on the decision and the receipt; add phase
   timestamps; show "why" on job and receipt views. No new measurement.
2. **Registry and parity harness.** Declare operations; a test walks every
   registered operation through each adapter with guest, member, app and
   owner credentials. Fix the two confirmed duplicated rules.
3. **Device and engine sampler, owner Now, member status.** Driver probes
   are optional data sources; absent data shows as unavailable.
4. **Plan.** Read-only, uses the same admission entry point as dispatch.
5. **CLI and owner MCP profile.** Generated client, curated commands.
6. **ETA.** Only after slice 1-3 data exists; record estimate error and
   suppress estimates that do not help.

## Not building

- A Prometheus/Grafana stack as a prerequisite, or raw engine `/metrics` as
  a user surface.
- A household leaderboard, per-person token rankings, or "you are #3"
  positions on batched backends.
- Invented GPU-seconds. Elapsed time, work units and billed cost only.
- One MCP tool per endpoint, or a code-execution MCP.
- Preemption and fair-share scheduling before simple concurrency caps and
  the display reserve prove insufficient.

## Open questions for the owner

- Default per-person concurrency for large media jobs.
- Whether members may see that a named household member is using the GPU,
  or only an anonymous class (default: anonymous).

# Headless surfaces and resource visibility (design v2)

Status: proposed design, not implemented. 2026-10-11.

v2 applies an independent red-team of v1 (`590dbdaa`): it cuts the
operation registry, the broad plan and the parity ritual, corrects what v1
assumed the code already does, and adds first-order household needs.

Evidence (divan `local-ai-gateway/`, 2026-10-11): a resource-observability
survey with a commit-pinned resource-facts audit; a headless-surfaces survey
with a commit-pinned parity audit; the v1 red-team; earlier GPU-contention
entries in `llm-serving/`.

## Problem

People cannot tell why work is slow or refused, whether a long job will
survive a closed tab, or when it is done. The owner cannot see what holds
the GPU. Many owner actions exist only in the browser. Agents and scripts
need the same lifecycle without a browser.

The gap is not only exposure. Today:
- Admission decisions keep reasons and codes but no structured capacity
  facts; free capacity appears only in failure prose, conflicting locks
  become a count, and rejections hide simultaneous delays.
- Missing load defaults to zero and missing availability to available, so
  "unknown" and "measured empty" look the same.
- Receipts and jobs have no phase timestamps; `updated_at` is overwritten.
- Job events use a global sequence, and clients can append arbitrary event
  types.
- Job idempotency deduplicates by request id globally, without checking the
  principal, operation or input.
- The admin API already accepts owner and admin bearer keys; there is no
  CLI; MCP has three data-plane tools.

## Principles

1. One authoritative server path for authentication, ownership, admission
   and dispatch. Adapters translate; they never decide policy.
2. Truth before estimates; unknown is never zero. Every fact carries
   `observed_at` and a quality (measured, declared, estimated, unavailable).
3. Separate quantities: device memory, engine KV-cache use, busy fraction,
   reserved capacity, request timings and billed cost.
4. Privacy by audience: members see their own work precisely and shared
   capacity only coarsely; owners see attribution, never prompt content.
5. Build for demonstrated needs. No framework, registry or ETA ahead of
   evidence.

## Release A: truthful outcomes, safe primitives

1. **Structured decisions.** Admission returns all reasons (rejections and
   delays together), the blocking resource class, and the capacity facts it
   used, each with `observed_at` and quality. Unknown inputs stay unknown
   and make admission conservative.
2. **Phase timestamps.** Record `accepted_at`, `queued_at`, `started_at`,
   `first_output_at`, `finished_at` prospectively on receipts and jobs, one
   timestamp per event with attempt numbers; legacy records show
   "unavailable". Lifecycle phases come only from trusted writers (the
   gateway and engine adapters), stored apart from client progress events.
3. **Safe reasons and next actions** on refusals and waits: wait (with a
   deadline), cancel my own work, use a permitted smaller or warm option, or
   opt into an already-authorized paid route. Never spend money or unload
   another person's engine without explicit consent. No invented queue
   position or ETA.
4. **Fixes found by the red-team:** per-job event sequence numbers (or
   opaque scoped cursors) in client responses; event appends limited to
   trusted writers; idempotency scoped to principal and operation, with a
   changed input under the same key rejected; ownership required, never
   optional, wherever private records are listed, applied before
   pagination.
5. **One admission gate on every dispatch path.** A trace at `33810bda`
   (`docs/ops/RESOURCE_ADMISSION_COVERAGE.md`) found admission consulted
   only by native media-job and async identity submissions, session Make
   (submission and worker start) and STT. Chat, the Anthropic and Ollama
   routes, embeddings, decisions and direct media requests never consult
   it, and regular media workers do not re-check at start. Route them
   through the same evaluator before dispatch, and re-check at worker start.
6. **Tests:** inference, member and app credentials cannot reach owner
   mutations through REST or MCP (mint key, raise budget, enable paid
   fallback, unload engine), with no side effects; no cross-principal
   leakage through lists, events, cursors or status.

## Release B: long jobs that survive

1. One canonical job state vocabulary with typed `is_terminal` and
   `holds_resources` predicates; MCP waits and outstanding-job limits use
   them.
2. Restart recovery and cancel semantics: "cancel requested" versus
   "stopped and released".
3. "Tell me when it's done": in-app notification first; output links that
   outlive the notice (renew signed URLs, show expiry).
4. A minimal hand-written CLI over the existing HTTP API: `auth`,
   `capabilities`, `run`, `jobs list|get|wait|cancel|output`, `receipts`,
   `status`, and `api` (raw call with the selected identity only, never an
   ambient owner key). JSON on stdout, NDJSON for events, progress on
   stderr, stable error codes, no prompts when not interactive.
5. MCP keeps `capabilities`, `run`, `job`; their results gain the phase and
   reason fields. No new tools yet.

## Release C: live capacity

1. Current snapshots per device (memory used, free and reserved for
   Azizam, the display reserve, host pressure, external use where the
   driver reports it) and per engine (residency, running and waiting
   counts), each dated. Driver probes are optional data sources. Short
   bounded history for diagnosis only.
2. Owner "Now": devices, active engines and jobs with person and app
   attribution, recent refusals and why, and "can I run X now" as ready /
   must load / can queue / incompatible / not allowed.
3. Member status: own work precisely; shared capacity as a coarse,
   rate-limited class with no other job ids, counts or timelines. Residual
   timing side channels in a small household are acknowledged, not denied.
4. A simple display reserve and per-person concurrency cap before any
   fair-share scheduler.

## Later, only on demonstrated need

- A narrow "explain availability" read using the same pure evaluation as
  dispatch, labelled "not reserved"; a fuller plan only if a client needs
  it. Dispatch always re-checks.
- Owner MCP tools on an explicit owner connection, default deny, passing
  the real owner credential to the same server authorization.
- A generated client once hand-written maintenance costs more.
- ETA ranges after phase data exists, with recorded estimate error.

## Support matrix, not a parity ritual

Each operation is listed with the surfaces that support it. Supported
surfaces get the same semantics tests; unsupported ones return an explicit
error. Permission-escalation and privacy cases are tested exhaustively. A
route inventory test (like the existing category route inventory) catches
operations someone forgot to list.

## Not building

A registry framework, a metrics platform as a prerequisite, household
leaderboards, invented GPU-seconds, queue positions on batched backends,
one MCP tool per endpoint, a code-execution MCP, or preemption before
simple caps prove insufficient.

## Open questions for the owner

- Default per-person concurrency for large media jobs, and the display
  reserve on the desktop GPU.
- Whether members may see that a named person is using the GPU (default:
  no, coarse class only).
- Where "done" notifications go beyond the app (none until asked).

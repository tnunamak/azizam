# Resource admission decisions and dispatch coverage

Release A implementation, based on `33810bda` and design v2 on
`design/headless-resources`. This inventory describes the checked source, not a
probe of a running gateway.

## Decision contract

Both pure evaluators retain their legacy summaries. Route decisions still use
`as_dict()`; claim decisions still use `summarize_decision()`. For fully supplied
inputs, their outcomes, reason precedence, codes and serialized fields remain
unchanged. The new `details` field contains every rejection, delay and
informational reason, including delays that coexist with a rejection.

Each structured reason has a code, effect, resource class, owner rule,
`observed_at` and quality. Each capacity fact has a name, resource class, value,
`observed_at` and quality. Timestamps are Unix seconds; a null timestamp means
that the producer supplied no observation time. Quality is `measured`,
`declared`, `estimated` or `unavailable`. Supplying a value does not make it a
measurement: inputs default to declared. A null value or unavailable quality
cannot become a zero or a known value during evaluation.

Numeric claims retain requested, total, used and available quantities. Available
capacity is total minus used only when both are known; its quality follows the
least certain input. Lock claims retain the conflicting subset for owners.
Queue/profile/app counts missing under an applicable limit cause refusal.
Missing occupancy for a positive capacity claim causes delay. An absent lock
observation differs from an explicitly empty set. Missing service availability
causes refusal. Unused capacity dimensions do not prevent unrelated work.

The media queue reader returns unknown if either database read fails. With a
queue limit, that now refuses work instead of treating a failed read as zero.
This is a read of queued/running job records, not a GPU measurement. STT records
measured disk free space and its process-local active count; its reserved disk
budget remains declared.

## Caller and owner views

`resource_decisions.CALLER_REASONS` is the shared dictionary for HTTP, receipts
and MCP. Callers receive plain reasons, coarse resource classes and allowed next
actions. Owner receipt views include the rules and facts. Operator prose,
conflicting lock names, quantities and observation timestamps are excluded from
the caller decision view. Existing admission error codes, status codes and
protocol envelopes remain; public reason text changes to remove private detail.

A refusal can offer a 60-second retry window, with an absolute deadline. This is
a caller waiting limit, not an ETA, queue position, promise of availability or
server-side queue. Legacy Retry-After headers are unchanged. The projection also
supports cancel-own-work and previously authorized alternatives when a trusted
route supplies them. Current dispatch callers supply no cancellation or model
alternatives because these admission inputs do not establish ownership or model
permission. Paid alternatives require explicit consent; projection never spends
money or unloads an engine.

Receipts store `resource_decision` only when a new evaluation was recorded. Old
rows are not backfilled. New image-submit, session-worker and STT receipts carry
their decisions. Refused audio/video/identity submissions get attributed refusal
receipts. Session submission refusals use an existing session App, or the
signed-in user's owner key, without creating an App. Receipt writes remain best
effort. The workflow adapter carries the already-projected refusal into MCP.

## Dispatch inventory

The direct call search is:

```sh
rg -n 'advise_resource_admission|evaluate_admission|advise_route_admission|admission_http_response' proxy.py gateway
rg -n '_media_job_admission_response|GLOBAL_STT_RESOURCE_GATE.acquire' proxy.py
```

| Dispatch path | Consults an evaluator? | Source and limits |
| --- | --- | --- |
| `/gateway/v1/media/jobs` with executable image/audio/music/sfx/video input | Yes, at submission | `_dispatch_gateway_media_job_submission` selects the image handler or audio/video submitter. They call `_media_job_admission_response`, which calls `advise_route_admission` → `advise_resource_admission`. |
| `/gateway/v1/media/jobs/image-generations` | Yes, at submission | `gateway_image_generations_media_job`; decision is attached to the existing route receipt. |
| Async identity-scene submission | Yes, at submission | `_identity_submit_job`; same route advisor, using the image intent. |
| Session Make image generation/editing | Yes, at submission and worker execution | `_SessionMakeRuntime.admission` and `.execute`; the worker skips the queue-depth check so it does not count itself. |
| Regular image/audio/video media workers and identity worker | No new evaluation at worker start | `_run_image_media_job`, `_run_audio_media_job`, `_run_video_media_job`, `_run_identity_job` dispatch through their existing executor paths. Submission advice does not reserve resources for later execution. |
| OpenAI audio transcriptions/translations, including `/openai/v1` aliases | Yes, on the bounded STT execution path | `_dispatch_selected_stt_route` → `proxy_stt_request` → `SttResourceGate.acquire` → `evaluate_admission`. Admission happens before multipart parsing and again if a changed service needs a replacement lease. Earlier format/configuration/storage checks can refuse before this evaluator. |
| Direct OpenAI image generation/edit/variation, audio generation, video creation/remix/edit/extension, speech | No | These handlers call their executors directly. Their other policy, availability and budget checks are separate from these two evaluators. |
| Chat/completions/responses, Anthropic messages, Ollama chat/generate/embed, embeddings and decisions | No | Neither evaluator is called on these paths. This does not mean those routes have no policy or budget admission. |
| MCP `run` | Depends on the selected route | `_mcp_internal_call` forwards the caller's headers through an in-process ASGI request. Native media-job submissions inherit that route's evaluation; direct media/speech/video calls inherit its absence. MCP does not add a scheduler or resource gate. |
| Native job records with no executable submission payload; discovery/list/status | No dispatch evaluation | These are storage/discovery paths, not reservations of execution capacity. |

No new dispatch gate, lock acquisition, scheduler or worker-start recheck was
added. Queue admission still has its existing check/submit race and scan bound.
The STT gate is process-local. This change makes recorded evidence truthful;
it does not establish global GPU capacity enforcement or stale-fact expiry.

## Verification scope

Regression tests distinguish unknown from measured zero, retain simultaneous
rejections and delays, compare legacy serialized outcomes, and test all four
fact qualities. Receipt round trips check owner evidence and caller redaction.
Route tests cover failed media queue reads, session attribution, STT's OpenAI
error envelope and MCP's structured refusal. No live engine, live database,
production port, gateway restart or browser was used.

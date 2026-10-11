# Resource admission decisions and dispatch coverage

Release A implementation and dispatch gate, based on `803f8350` and design v2 on
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

All executable routes below reach `resource_admission_http.advise_route_admission`
→ `resource_admission.advise_resource_admission`. STT also retains its separate
`resource_claims.evaluate_admission` lease for disk and process-local concurrency.
The new dispatch helper is at `proxy.py:21966` (`_dispatch_admission_response`); the existing
media submission helper is at `proxy.py:22056` (`_media_job_admission_response`).

| Dispatch path | Admission point | Source |
| --- | --- | --- |
| Native executable image/audio/music/sfx/video job submission | Before queue submission; existing evaluator | `proxy.py:22056` (`_media_job_admission_response`) |
| Regular image worker | After acquiring a worker turn, before execution; each image retry also enters the synchronous gate | `proxy.py:21610` (`_run_image_media_job`) |
| Regular audio and video workers | After acquiring a worker turn, before execution | `proxy.py:22247` (`_run_audio_media_job`), `proxy.py:22719` (`_run_video_media_job`) |
| Identity jobs and synchronous identity/likeness routes | Submission, worker start, and the native route's policy/admission boundary | `proxy.py:15134` (`_identity_submit_job`), `proxy.py:15186` (`_run_identity_job`), `proxy.py:15266` (`_identity_admission`) |
| Session Make | Submission and worker execution; synchronous executor checks again | `proxy.py:14497` (`_SessionMakeRuntime`) |
| OpenAI chat, completions and embeddings, including aliases | Before dispatch preparation; selected local, current-loaded and paid paths check before handing off to transport | `proxy.py:29136` (`_handle_openai_litellm_request_once`), `proxy.py:28673` (`_forward_litellm_proxy_request`), `proxy.py:28371` (`_forward_current_loaded_openai_request`) |
| Internal local chat execution | Before engine transport; outer caller admission remains authoritative for app attribution | `proxy.py:29326` (`_forward_internal_litellm_local_upstream`) |
| OpenAI Responses | Before protocol loopback; underlying chat dispatch checks again | `proxy.py:27249` (`create_response`) |
| Anthropic Messages | Before protocol loopback; underlying chat dispatch checks again | `proxy.py:26396` (`anthropic_messages`) |
| Ollama chat/generate and both embedding APIs | Before protocol loopback; underlying chat/embedding dispatch checks again | `proxy.py:26566` (`_ollama_forward_non_streaming`), `proxy.py:26610` (`_ollama_forward_streaming`), `proxy.py:26683` (`_ollama_forward_embeddings`) |
| System One and OpenAI Decisions | Before dispatcher handoff; local System One rechecks each selected fallback before transport | `proxy.py:14844` (`_dispatch_decision`), `proxy.py:14872` (`_openai_decisions`), `gateway/decision_routing.py:183` (`DecisionRuntime._dispatch_before_deadline`) |
| Synchronous images: generation, edits, variations | At executor entry and after asynchronous preparation, before submission | `proxy.py:8075` (`proxy_comfy_request`), `proxy.py:8512` (`proxy_comfy_edit_request`), `proxy.py:9142` (`proxy_comfy_variations_request`) |
| Synchronous audio generation | At executor entry and immediately before submission | `proxy.py:24225` (`proxy_audio_generate`) |
| Synchronous video creation, remix, editing, extension, typed workflow editing | At executor entry and after asynchronous preparation, before submission | `proxy.py:10904` (`proxy_comfy_video_create`), `proxy.py:12704` (`proxy_comfy_video_remix`), `proxy.py:12768` (`proxy_comfy_video_edit`), `proxy.py:11895` (`proxy_comfy_video_extend`), `proxy.py:23698` (`_execute_video_manifest_workflow`) |
| Speech synthesis | Default and selected service checks, before readiness/transport; each fallback enters again | `proxy.py:4367` (`_proxy_tts_once`) |
| Transcriptions/translations | Operator admission before selected service readiness, plus existing resource-claim lease before upload parsing/dispatch | `proxy.py:4819` (`_dispatch_selected_stt_route`), `proxy.py:4996` (`proxy_stt_request`) |
| Realtime WebSocket and provider-native passthrough | Before backend transport | `proxy.py:18998` (`_handle_realtime`), `proxy.py:30003` (`provider_native_request`) |
| MCP, A1111 and other protocol adapters | Inherit the destination route's admission through their internal request | `proxy.py:14740` (`_mcp_internal_call`) |
| Discovery, status, stored-output retrieval, unsupported APIs, native job records without executable input | No inference dispatch; no capacity reservation | Route inventory distinguishes these from execution. |

## Configuration and facts

The existing `resource_admission.media_jobs` configuration and the owner
`/admin/api/operator/media-job-admission` overrides now apply at dispatch too.
No second policy store was added. `max_app_concurrency` maps app ids to integer
limits and is accepted by that existing owner API. Resource classes are `chat`,
`embeddings`, `decision`, `image`, `audio`, `video`, and `native`. Frontends use
existing policy intent names (for example `chat.messages`, `chat.responses`,
`speech.synthesis`, and `decision.systemone`); internal forwards check their own
canonical operation as well.

With no configured limits or service override, dispatch still calls the evaluator
but does not require a service-availability observation. This explicit constraint
preserves the previous route's availability/error behavior, including response
bytes and headers. It does not turn unknown availability into true. Existing
callers keep the evaluator's strict default. Clearing all limits also restores
this behavior. No new decision evidence is attached to an unconfigured direct
request's receipt.

When admission is configured, availability comes from the selected service's
configuration or explicit owner override and is labelled declared. A missing
service remains unknown and refuses. The helper does not probe GPU memory,
engine occupancy, or running requests. Direct media uses the existing database
queue reader. Queue and app limits apply only at media admission boundaries:
`max_queue_depth` counts queued/running media jobs; `max_app_concurrency` counts
that app's queued/running media jobs in SQLite when the request has app attribution.
The app count is not truncated by the history scan limit. Database read failures
remain unknown and refuse work under an applicable limit.

Chat, embeddings, decisions, speech, transcription, realtime and provider-native
direct dispatch do not join this media queue and have no active-request counter.
Queue/app limits therefore do not apply to those paths. They still enforce intent,
resource-class, token and selected-service policies. These limits do not measure
or cap the concurrency of synchronous requests or unattributed Make submissions;
Make also retains its own atomic per-person/household outstanding-job limits.

Worker checks run after the worker semaphore is acquired. They omit the queue
and app limits because the job is already counted in that queue. A scoped context keeps nested
synchronous checks from counting it again and is reset even on cancellation.
A refused worker is marked failed before its executor runs; it does not start an
unbounded retry loop or silently wait for resources.

Refusals use the existing 503 admission status and Retry-After behavior. OpenAI
routes return an error object, Anthropic returns `type: error` with an error
object, and Ollama returns its string-valued `error`. Each includes the caller
projection and admission summary. Protocol translation preserves these fields
if a later inner dispatch recheck refuses after the outer check passed.
A degraded decision retains the existing evaluator's allowed/degraded semantics.

Admission is advice plus a refusal gate, not an atomic reservation. Queue
check/submit races and the queue scan bound remain. App limits are advisory
checks against durable jobs, not atomic concurrency reservations. This change adds
no scheduler, global GPU accounting, capacity measurement or stale-fact expiry.

## Verification scope

`tests/test_dispatch_admission.py` compares 13 successful requests against
`tests/fixtures/dispatch_admission_baseline.json`, captured from unchanged
production source at `803f8350` by test-only commit `e48d86a4`. It compares raw
response bytes, every returned header and status. Inputs, backend replies,
clocks and generated ids are fixed before execution; responses are not
normalized. Cases cover streaming and buffered chat, Anthropic and Ollama,
OpenAI/Ollama embeddings, both decision surfaces, synchronous image generation
and speech. The fake transports are below the routing/translation code, except
media and decision executors which are replaced at their execution seams.

Before the implementation, all 13 corresponding disabled-resource-class tests
failed because they reached their executor. Afterward, refusals prevent that
execution. Additional tests cover direct traffic with inapplicable queue/app limits, measured media-app occupancy,
intent limits, service overrides, caller redaction, late streaming refusals,
policy changes during image preparation and speech service startup,
worker-start changes and the worker queue exemption.

`tests/test_route_policy_inventory.py` discovers model-facing mutation routes
and probes them with resource classes disabled. Backend dispatch and unexplained
success fail the test. A synthetic unguarded route proves the inventory catches
a newly added bypass. Validation/configuration refusals are not presented as
proof that a successful execution path was exercised; the differential cases
supply that evidence. The local decision fallback tests prove that a refused candidate is neither
activated nor called, and that policy is checked again after readiness.

Tests use the hermetic Python runner, with no live engine/database, production
port, gateway restart, browser test or web build. These checks do not establish
live-engine behavior, distributed concurrency enforcement, or all possible
client inputs.

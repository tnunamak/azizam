# Code to data, S7 parity addendum: the image, audio and video routes onto rows

Status: design, owned by the integrator on `release/c2d-int`. It completes section 8 of
`docs/design/CODE_TO_DATA_S4_S8.md` for the routes that did not move in the first S7 round (images, audio, video).
Mesh and the identity graph moved in that round.

## 1. Why this addendum exists

The freeze said each route moves "onto its frontend row over `submit_run`" and listed the golden differences it
allows. It did not list what each old route does. Builders D5b and D6 found the gap: the run core path had no receipts,
no History run facts, no wire options, no asset origins, admission after the job existed, and different engine
traffic. So no image, audio or video route moved.

This round starts from a complete list. A read-only pass over the old routes at 7daa31769 listed every observable
behaviour with file:line, under eleven categories: 1 validation, 2 admission, 3 wire options, 4 engine traffic, 5
receipts, 6 History, 7 Library rows, 8 response, 9 job lifecycle, 10 restart and recovery, 11 failure text. The lists are
in `docs/design/c2d-s7-parity/`: `images.md` (114 items), `audio.md` (84), `video.md` (128) and `writers.md` (the
receipt, History and Library writers the routes call).

**The rule:** every item in those lists becomes a parity test before its route moves. The test runs the old route and
the row path on the same request and compares the observable behaviour the item names. It is written first, and it
must fail on the row path at the base, unless the item already matches; then it pins the match. A route moves when all of
its tests pass, or when an item is on the allowed-delta list in section 4. No item is closed by a mask, a skip or a
manifest entry that is not on that list.

## 2. Mechanisms

Each mechanism says what it is, where it lives, and which inventory categories it answers. The interface items are
additive: a new optional field or a new optional port method. No frozen signature changes.

### M1. Receipt and History at the row server (category 5, 6)

Builder RH ships this in the current train for mesh (brief `RH-receipt-history`): `RowRouteDeps.open_receipt`, one
receipt per request closed once with the outcome, `owner.handoff()` for an async answer and the core closing it at
terminal, `RunRequest.request_id`, the History run facts on `request.state`, and restart recovery closing an open run
receipt as `GATEWAY_RESTARTED`.

Addition for this round: **where the receipt opens is row data.** The old image routes open the receipt before the
route policy (images-29), so a policy denial leaves a closed `rejected` receipt and its retention facts (golden
`s7_ig_policy_refusal`). The old mesh route opened none for a denial. A row gains `receipt_opens: before_policy |
after_policy` (default `after_policy`). Each image, audio and video row sets the value its old route had; the parity test
of the policy-denial item decides it.

The receipt fields the old routes wrote (`accepted_inputs` in their order, `resolved_workflow`, `manifest_hash`,
`image_size_adjustment`, the models-freed flag, the firehose event name) come from the writers in
`gateway/workflow_run.py`, fed by the run request and the adapter's result metadata (M5). The event name is the row's
`receipt_protocol`.

### M2. Wire options travel on the run request (category 3)

`RunRequest` gains `options: Mapping[str, Any] = {}`: the transport options a caller chose on the wire that change what
the engine is asked for (`n`, `size`, `output_format`, `background`, `quality`, the audio container, video `seconds`
when it is not a workflow input). The shape fills it; the core includes it in run identity (so a replay with other
options is a conflict, as today); the run port passes it to the adapter in the flat body under `options`. The answer
form that changes only the rendering (images `response_format`) stays on the wire request (13.2 addendum) and is not an
option.

Clamps and checks the old routes made on these values (`n` clamped to 10 with a warning, `size` grammar) stay in the
shape, because they are wire vocabulary.

### M3. Asset origins and file names (category 7)

The core's result writer stores each Library asset with the origin the old route wrote:
`{request_id, takes_text, workflow_name}` (images-36 and its audio and video peers), from `RunRequest.request_id` and the
chosen entry. The stored file's extension comes from the output's `media_type` (or the sniffed container for audio,
audio-75), never `.bin` when the type is known. The asset kind prefix (`img_`, `aud_`, `vid_`) comes from the output's
media type family, as today.

### M4. Admission refuses before the job exists (category 2)

Today the row path creates the job and then fails it when the service is disabled, so the caller gets 200 and a failed
job where the old route answered 503. The fix: the run port's `binding_problem` (S3) also answers the service's live
state, as reason codes: `service_disabled`, `service_unhealthy`, `service_not_admitting` (with a retry delay).
`target_gate` turns a reason into `RunRefused(503, code, message, retry_after_s=...)` before `submit_run`. The shape
renders it in its own vocabulary: the text table from reason code to wire text ("Image generation not enabled",
`service_unavailable`, `Retry-After: 30`) is the shape's code, keyed by reason code and never by service name.

Capacity at dispatch stays the adapter's internals, as the mesh addendum decided: a busy pool fails the job with
`resource_unavailable`, and a sync row renders that failure as the old 503 with `Retry-After`.

### M5. The adapter's failure and result facts reach the answer (category 8, 11)

The node-graph adapter returns the old failure text in `RunFailure.message`, with `upstream_status` and a `code` from
the old vocabulary (`image_generation_timeout`, `upstream_out_of_memory`, `image_client_disconnected`, ...). The engine's
display label reaches the text only through the binding's service label, as today (`{label} returned no images`).

Facts the old route put in headers or receipt metadata (`X-Image-Rounds`, `X-Image-Returned`, `X-Proxy-Warning`, the
background-removal pair, `X-ComfyUI-Models-Freed`, the size adjustment) come back as the job's result metadata, a
mapping the adapter fills and the core stores on the job. The shape renders the headers from it; M1's receipt writer
copies the receipt fields from it. The core does not interpret it.

A sync row whose old route answered an engine failure synchronously (video create: 502) renders a failed job as that
status. That is the shape's rendering of a terminal job, not a new mode.

### M6. Engine traffic parity in the node-graph adapter (category 4)

The engine calls the old routes made move into the node-graph adapter as they are, not rewritten: the round
(`_graph_run_round`: submit, poll history and queue, view each output), the poll schedule (0, 0.05, 0.1, 0.2, 0.4,
then 0.5 s), the health probe if the old route made one, `/free` and one retry on out-of-memory, cancel on client
disconnect and on task cancellation (with the same `kind`), cancel on a wait timeout, and adaptive batching for `n > 1`
(rounds up to `n + 2`, stop on an empty round). The goldens' `engine_requests` and `gateway_sleeps` are the test. The
poll schedule and round limits are the adapter's internals; they are not binding data.

### M7. The video lifecycle routes are the video shape's job views (category 9, 10)

`GET /v1/videos/{id}`, `GET /v1/videos/{id}/content`, `GET /v1/videos` and `DELETE /v1/videos/{id}` become routes of the
video row that render `JobService` views. The wire id stays `video_<id>`: the shape adds the prefix to the job id and
strips it on lookup, and a malformed id is the old 400 `invalid video_id`. Visibility and ownership are the job store's
(S2), which already hashes the owner key.

Old records in `video_job_records` (ids `video_<engine prompt id>`) are served by a read-only legacy reader in the video
shape, for lookup, list, content and delete, until the table is empty. No new record is written there. A ratchet counts
the legacy reader's call sites and the table's live rows; the reader and the table go when the count is zero.

### M8. Row-declared refusals (category 1, 2)

A row may declare refusals that are data, not code: `unconfigured: {status, code, message}` answers when the row's
selector chooses no runnable workflow. `videos.edit` today answers 501 `video_remix_not_configured`; its row declares
that, so the route keeps the answer without a handler.

### M9. The chain and subject-select graphs become templates (category 3, 4)

Chain (`gateway/video_extend.py`, `gateway/video_extend_h3.py`): each chain workflow becomes a template with one
declared **segment** sub-graph and node roles (`context_in`, `latent_out`, `prompt`, `seed`, `frames`), plus binding
data for the frame grid (`frame_step`, `frame_offset`), the context windows, the shot minimum and the negative default.
A generic segment composer in the gateway-compose protocol (`compose: segment_chain` in
`registry/protocols/gateway-compose.yml`) repeats the segment `N` times and wires each `latent_out` to the next
`context_in`. Model files and node classes live in the template JSON. The test: the graph the engine receives for each
`video_extend*` golden is byte-identical after canonical sorting, as D7 did for identity. Source checks that are
security or wire limits (`check_source`, 24 fps) stay code.

Subject select (`gateway/video_subject.py`): the node-id constants become node roles in the
`video-scail-2-replace` binding (it already binds `subject_index`). Selection validation stays code (wire vocabulary).

`_meta` leaves video: the roles `_proxy_graph_video_create` reads from `_meta` become the binding's node roles, as D7 did
for identity. A missing role is a template problem at load (video-114), not a 400 at request time; that is on the
delta list (section 4).

### M10. The shared intent tables become snapshot facts

`_MODEL_LISTED_MEDIA_INTENTS`, `_DISCOVERY_OPERATION_INTENTS`, `_MEDIA_JOB_RESOURCE_CLASS_BY_INTENT`,
`_AUDIO_MEDIA_JOB_INTENTS`, `_VIDEO_MEDIA_JOB_INTENTS`, `_VIDEO_GRAPH_CAPABILITY`, `_AUDIO_GRAPH_CAPABILITY`: each read
becomes a read of the entry's declared intent, outputs or `resource_class` (section 6.3), or goes with the handler that
read it. Any that stays is listed in the ratchet's reviewed-site list with its reason.

### M11. Job failure copy per category (category 11)

`_JOB_FAILURE_COPY` (authorization, timeout, upstream with status, capacity, safety, invalid input) is wire vocabulary
of the media-job shape. The shape renders a failed job's `RunFailure.category` with it. The core stores the category and
the adapter's message; it does not render copy.

### M12. Restart recovery parity (category 10)

The core's recovery gives each state the old text: payload missing ("... payload missing after proxy restart"),
recovery turned off, cancel requested before the restart (failed, not cancelled), running (failed, never re-run),
queued (rescheduled), crash (`_MEDIA_JOB_CRASHED`, retryable). The texts are one table in the core, keyed by recovery
outcome (`RecoveryOutcome`), not by intent; an intent-specific text that differs today is unified on the image text
and listed in section 4.

## 3. Builders and ownership

At most four children at once (Sonnet, medium). Each writes the parity tests for its items first, in
`tests/test_s7_parity_<domain>.py`, red on the base, and reports the red count before changing code.

| Builder | Owns | Items |
|---|---|---|
| K (core) | `RunRequest.options`, M2 identity, M3 writers, M4 port reason codes and `target_gate`, M5 result metadata on the job, M12 recovery table, `receipt_opens` row field | categories 5-7, 10 of every domain; writers.md |
| I (images) | image shapes, the node-graph adapter's image round and batching (M6), image header rendering, image rows | images.md |
| A (audio) | audio shape and container handling, audio rows, `_JOB_FAILURE_COPY` move (M11) | audio.md |
| V (video) | video shape, lifecycle views and legacy reader (M7), M8, M9 templates and the segment composer, video rows, M10 | video.md |

K's interface items land first, as a small commit on `release/c2d-int`, before I, A and V branch, so all three code
against the same fields. M10 goes to V because most of the tables are read by video and audio handlers; A reports any
audio-only read to V.

When a domain's routes move, its old handlers, runners and schedulers are deleted in the same commit, and the golden
rig's runner names and scheduling captures are updated with them.

## 4. Allowed deltas (added to `tests/support/adapter_differential_deltas.py`)

Each is a behaviour the new path changes on purpose. Each needs a manifest entry with exact paths, old and new values,
and a reason. Nothing else may differ.

- `idempotency_acceptance` (already listed) now also covers video create: the old direct route ignored
  `Idempotency-Key` (video-05, golden `s7_vc_replay_same`); the row dedupes.
- `submit_validation`: the old audio job route accepted an invalid body with 202 and failed the job later (audio-35);
  the row refuses it with the shape's 400 at submit.
- `persistence_failure_visible`: the old video create swallowed a failed record save and still answered 200
  (video-16); the job store's write is the job, so a failed save is a 500 and no job.
- `template_role_at_load`: a template missing a declared node role is a load problem, not a request-time 400 (video-114).
- `restart_closes_receipt`: a sync request cut by a restart left no receipt row (images) or an `in_progress` row that
  reads as `stale_incomplete` (audio, video) (writers.md, images-R07); every row's receipt is now closed as
  `GATEWAY_RESTARTED` by startup recovery (M1).
- `recovery_text_unified`: where audio or video recovery text differs from the image text for the same outcome, the
  image text wins.

Any other delta found while building is reported to the integrator with its item id; it is not added by a builder.

## 5. Done means

Every inventory item has a parity test that passes, or is on section 4. The old image, audio and video handlers,
runners and schedulers are gone. The domain-literal and engine-knowledge ratchets do not rise. The full hermetic suite,
the goldens under oracle profile 2, and the route baseline pass.

# Audio route inventory (audio group)

Code: `origin/release/c2d-int` at `7daa31769`, read in worktree (a local spike file, not committed). Line numbers are from that commit.
Spike diff: (a local spike file, not committed) covers only `audio_generate_sync` (success case, 50 differences).
No spike diff exists for the job route, the content, results, events, cancel or list routes, or any failure case.
So "row path" is `unknown` for every item outside the sync success case.

Category key: 1 validation, 2 admission, 3 wire options, 4 engine traffic, 5 receipts, 6 History, 7 Library asset rows,
8 response and headers, 9 job lifecycle and ids, 10 restart and recovery, 11 failure text.

Routes covered:
- R1 `POST /v1/audio/generate` (sync), 32 items
- R2 `POST /gateway/v1/media/jobs` with `audio.generate`, `music.generate`, `sfx.generate`, 21 items
- R3 `GET /gateway/v1/media/jobs/{job_id}`, 3 items
- R4 `GET /gateway/v1/media/jobs`, 2 items
- R5 `GET /gateway/v1/media/jobs/{job_id}/content`, 3 items
- R6 `GET /gateway/v1/media/jobs/{job_id}/results/{name}`, 1 item
- R7 `GET` and `POST /gateway/v1/media/jobs/{job_id}/events`, 2 items
- R8 `POST /gateway/v1/media/jobs/{job_id}/cancel`, 4 items
- R9 Job runner, scheduler and restart (no HTTP route), 16 items

Out of scope: `GET /v1/audio/models` (`audio_models`, proxy.py:27593) lists models; it does not generate audio.

---

## R1: `POST /v1/audio/generate` (synchronous)

Handler: `audio_generate` (proxy.py:27554) calls `proxy_audio_generate` (proxy.py:27238).

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| audio-01 | 2 | Caller authentication runs first and returns the authenticator's response unchanged when it fails. | proxy.py:27556-27559 | unpinned | unknown |
| audio-02 | 2 | Route policy for `/v1/audio/generate` runs before any receipt starts and refuses with a policy response. | proxy.py:27562-27564 | s7_as_policy_refusal.json | unknown |
| audio-03 | 5 | A receipt opens before the handler and closes with `finish_response` (or `finish_receipt` on error, status 499 when cancelled, else 500). | proxy.py:27566-27575 | s7_as_* receipt fields (audio_generate_sync.json) | differs: spike shows receipts absent or differing (`$.receipts.receipts`) and row mode `workflow.run` vs `sync_media_route` |
| audio-04 | 2 | Admission runs before the body is read: `_dispatch_admission_response("audio.generate", resource_class audio, queue comfyui-media)`. | proxy.py:27249-27254 | s7_as_admission_refusal.json | unknown |
| audio-05 | 2 | When COMFY is disabled the refusal is 503 `service_unavailable` with message "Audio generation not enabled". | proxy.py:27263-27268 | s7_as_admission_refusal.json | unknown |
| audio-06 | 2 | An operator override that sets the graph service to disabled, maintenance or draining gives 503 `backend_unavailable` with `service` and `state` keys and `Retry-After` (default 10 seconds). | proxy.py:18920-18945; call at proxy.py:27270-27273 | unpinned | unknown |
| audio-07 | 1 | A non-JSON body gives 400 `invalid_request_error` "Invalid JSON body". | proxy.py:27275-27281 | s7_as_validation_refusal.json | unknown |
| audio-08 | 1 | A top-level field the audio grammar does not know is refused with the field's reason, and the reason never names the field. The 400 body is pinned by no test; only the detector is pinned. | proxy.py:27285-27288; `gateway/audio_extensions.py` | test_media_job_errors.py::test_the_audio_unknown_field_reason_never_names_the_field (detector only) | unknown |
| audio-09 | 1 | A metadata-mode body error (`_note_media_metadata_body`) returns its own response before the graph is built. | proxy.py:27289-27292 | unpinned | unknown |
| audio-10 | 1 | `audio_graph.check_request` refuses an invalid body with 400 and the error's `param`. | proxy.py:27293-27296 | unpinned | unknown |
| audio-11 | 1 | An unknown model gives 404 `model_not_found`, `param` "model", message "Model '<m>' is not available for audio generation." | proxy.py:27296-27303 (`model_compatibility_error`) | unpinned on sync (job route pinned: see audio-39) | unknown |
| audio-12 | 1 | A blank model resolves to the default workflow through `_resolve_audio_workflow`. A model key that is present but null is passed through `.get`; the resolver is the only thing that decides it. | proxy.py:27283 and `_resolve_audio_workflow` (proxy.py:27102) | unpinned | unknown |
| audio-13 | 3 | `type` (default "music") goes to the response header `X-Audio-Type` and to provenance `parameters.type`. | proxy.py:27242, 27489-27490, 27516-27521 | audio_generate_sync.json (`x-audio-type` "music") | differs: spike shows header `x-audio-type` absent on the row path |
| audio-14 | 3 | `duration` goes to the manifest inputs and to provenance `parameters.duration`; limits come from the deployment's `audio.workflows` config (`default_duration`, `min_seconds`, `max_seconds`). | proxy.py:27319-27333, 27516-27521 | audio_generate_sync.json (success path only) | differs: the engine request body text differs (content-length 492 on the row path vs 494 in the golden); the spike diff does not show which field |
| audio-15 | 3 | `seed` comes from `generated_defaults.first_value` over the effective manifest and goes to provenance `seed`. | proxy.py:27335, 27518 | audio_generate_sync.json (success path only) | differs: same engine body diff as audio-14 |
| audio-16 | 3 | Every other manifest input is taken from the public body by `audio_graph.manifest_inputs` against the effective manifest; a value outside the manifest is refused as an input error with the manifest's `param`. | proxy.py:27317-27327 | unpinned beyond the success golden | unknown |
| audio-17 | 4 | The engine submit posts the compiled graph with `client_id` and the configured downstream headers; a non-200 gives 502 `upstream_refused` with the adapter label as subject. | proxy.py:27388-27395 | unpinned (`upstream_refused` has no test or golden hit) | unknown |
| audio-18 | 4 | A submit reply with no `prompt_id` gives 502 "No prompt_id returned" (api_error). | proxy.py:27396-27402 | unpinned for audio (`image_prompt_without_id.json` covers the image route) | unknown |
| audio-19 | 4 | Polling runs at most 600 times, and each poll is preceded by `asyncio.sleep(2)`; it calls `history(prompt_id)` and retries on a non-200 reply or when the prompt id is not yet in the history. | proxy.py:27407-27416 | audio_generate_sync.json (one sleep of 2.0 in the golden) | differs: spike shows 26 sleeps of 0.1 on the row path, and history is requested at `/history/p1` |
| audio-20 | 4 | On engine status `success`, the output is picked by `adapter.collect` with the manifest's output selector and the result limits. A `result_missing` refusal yields no output; any other selector refusal gives `_result_refusal_response(code, 502)`. | proxy.py:27420-27434 | test_result_bundle_audio.py (result limits; not audio-specific per item) | unknown |
| audio-21 | 4 | The file is fetched from `/view` with `filename`, `type` and, when present, `subfolder`, under `FETCH_GATE`; a `FetchRefused` gives `_result_refusal_response(code, 504)`; a non-200 `/view` falls through to "No audio output found". | proxy.py:27436-27452 | audio_generate_sync.json (`/view` with 2 query params) | differs: spike shows engine request 2 is `/history/p1` with 0 query params, not `/view` with 2 |
| audio-22 | 4 | On engine status `error`, the reply is 502 with the first `execution_error` text (via `_graph_error_message`), or "<label> generation failed" when there is none. | proxy.py:27500-27512 (`err_msg` loop) | audio_generate_execution_error.json | unknown |
| audio-23 | 4 | Polling exhausted after 600 polls gives 504 "Audio generation timeout" (api_error). | proxy.py:27531-27536 | s7_as_wait_timeout.json; s7_as_disconnect.json | unknown |
| audio-24 | 4 | A transport timeout gives 504 "<label> request timeout". | proxy.py:27538-27544 | unpinned for audio (`request timeout` hits only TTS/STT tests) | unknown |
| audio-25 | 4 | A transport `unreachable` gives 502 with `adapter.unavailable_message(...)`. Its exact text was not verified. | proxy.py:27545-27548 | audio_generate_unreachable.json | unknown |
| audio-26 | 11 | Any other exception in the engine block gives 502 `audio_failed`: "The audio engine failed while handling this request." | proxy.py:27549-27550; `gateway/client_errors.py` REASONS | unpinned | unknown |
| audio-27 | 3 | `normalize_peak_db` in the workflow config rewrites the audio bytes in a private-tmp worker before delivery. | proxy.py:27495-27500 | unpinned | unknown |
| audio-28 | 8 | Delivery attaches provenance (kind audio, request id `audio_<prompt_id>`, resolved model, workflow id, backend, seed, duration, type); with metadata mode `none` the bytes go through `deliver_bytes` instead. | proxy.py:27501-27524 | audio_generate_sync.json | differs: asset and sidecar names and suffixes differ on the row path (see audio-31) |
| audio-29 | 11 | A metadata rewrite failure gives 502 "Could not prepare audio for delivery" (api_error). | proxy.py:27526-27532 | unpinned | unknown |
| audio-30 | 8 | Success returns the raw bytes with the engine's `Content-Type` (default `audio/wav`) and headers `X-Audio-Model: <workflow name>` and `X-Audio-Type: <type>`; the response carries `selected_result`. | proxy.py:27525-27545 (`delivered`) | audio_generate_sync.json (`x-audio-model` "tangoflux", `x-audio-type` "music") | differs: spike shows `x-audio-model`, `x-audio-type` and `x-request-id` absent on the row path |
| audio-31 | 7 | Library asset rows: with a receipt and no `store_full`, `media_ingestion.store_audio_response` stores the bytes as kind `aud`, file `audio<suffix>`, supported media types only (suffix map in media_ingestion.py:347-410). | media_ingestion.py:284, 347-410; proxy.py:27508-27511 | audio_generate_sync.json (`assets`) | differs: row path writes `<sha>.bin` and sidecar `<id>.meta.json` in a different order; golden expects `<id>.wav` and `<id>.meta.json` |
| audio-32 | 5 | Retention facts are captured before the engine submit (`_capture_retention_facts`) and `receipt.backend` is set to the comfy backend. | proxy.py:27357-27363 | audio_generate_sync.json (`retention_fact_*` rows) | differs: spike shows `$.media_jobs.retention_fact_destinations`, `retention_fact_references` and `retention_fact_revisions` differ |

The sync route has an upstream media type that the store does not accept. For that case, no asset row and no job row are written (media_ingestion.py:284). Item audio-31 covers the accepted types only; the refusal case is unpinned.

Counted R1 items: 32.

---

## R2: `POST /gateway/v1/media/jobs` with audio intents

Dispatch: `create_media_job` (gateway/api.py:670) calls `_dispatch_gateway_media_job_submission` (proxy.py:25803), which calls `_submit_audio_media_job` (proxy.py:24994) when the body is audio.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| audio-33 | 1 | A payload with no intent gives `None`, so the dispatcher returns to the caller's own handling. | proxy.py:25803-25810 | unpinned | unknown |
| audio-34 | 1 | An intent that is not image, audio, video, or a known generation intent gives 400 `unsupported_media_job_intent`, with `supported_intents` [image.generate, audio.generate, music.generate, sfx.generate, video.generate] and message "Media job intent '<intent>' is not executable yet. Currently supported: ...". | proxy.py:25778-25797, 25830-25833 | test_image_media_jobs.py (code string) | unknown |
| audio-35 | 1 | Submit does not run `audio_graph.check_request`, so a bad body gets 202 and fails later in the runner (see audio-86). | proxy.py:24994-25140 (no `check_request` call) | unpinned | unknown |
| audio-36 | 2 | Gateway auth runs first and returns its response unchanged when it fails. | proxy.py:25000-25003 | unpinned | unknown |
| audio-37 | 2 | The audio job policy `_audio_media_job_policy()` runs against `/gateway/v1/media/jobs` and a denial returns the policy response (403 `not_in_access_plan`). | proxy.py:25005-25009 | s7_aj_policy_refusal.json; test_media_job_errors.py | unknown |
| audio-38 | 1 | The model is read as `str(body.get("model", default))`, so a null model becomes the string "None" and is refused by the resolver with 404 `model_not_found` (job path) where the sync route falls back to the default. | proxy.py:25012-25018 | unpinned | unknown; differs from R1 (audio-12) |
| audio-39 | 1 | An unknown model gives 404 `model_not_found`, `param` "model", "Model '<m>' is not available for audio generation.", through `model_compatibility_error`. | proxy.py:25016-25023 | test_audio_media_job_integration.py::test_generic_submit_rejects_unknown_audio_model | unknown |
| audio-40 | 2 | The generic policy decision for `audio.generate.<workflow>` runs before admission; a denial returns the policy response. | proxy.py:25026-25041 | s7_aj_policy_refusal.json | unknown |
| audio-41 | 2 | Admission `_media_job_admission_response(intent)` runs before the job row exists; it may refuse with 503 capacity and writes a rejected receipt. The audio-disabled reply on the job route is not pinned. | proxy.py:24554 (`_media_job_admission_response`), called at proxy.py:25042-25044 | unpinned on the job route | unknown |
| audio-42 | 2 | The workflow target policy (`_workflow_target_policy_decision`) runs after admission and a denial returns the policy response. | proxy.py:25045-25051 | unpinned | unknown |
| audio-43 | 3 | The body becomes job metadata through `audio_job_from_request` (model, duration, seed, response_format, type, tag_count). | gateway/audio_media_jobs.py (`audio_job_from_request`); proxy.py:25052-25063 | s7_aj_* goldens (job_json) | unknown |
| audio-44 | 3 | `metadata.mode` is set to `async_audio_generation`; the resume and recovery code depends on this value. | proxy.py:25066-25068 | audio_generate_job.json | unknown |
| audio-45 | 3 | The manifest is loaded for the workflow; when present, the generated block is computed. A `ManifestError` from the generated block returns `_generated_value_fault_response`. | proxy.py:25073-25084 | unpinned | unknown |
| audio-46 | 9 | A job id has the form `mj_<uuid4 hex>`; a request id has the form `audjob_<uuid4 hex>`. | proxy.py:24997 (request id); gateway/media_jobs.py:169 (`new_job_id`) | unpinned for the audio route (`test_media_job_identity.py` exists; coverage not checked) | unknown |
| audio-47 | 4 | The envelope is recorded when the manifest has a pin; `needs_envelope` is true only when the job holds a generated value. A failure to build it gives 503 "Could not pin the workflow definition" (`_pin_not_recorded_response`, proxy.py:6526). | proxy.py:25085-25093 | unpinned (no test or golden hit on "Could not pin") | unknown |
| audio-48 | 2 | The payload is persisted to the ephemeral spool before the row is created. A spool capacity error deletes the spool entry and gives 503 `media_payload_capacity`. | proxy.py:25097-25109, 23303 | test_prompt_memory_lifecycle.py (`media_payload_capacity`) | unknown |
| audio-49 | 9 | `media_jobs.create` writes the job row; the envelope-not-recorded error gives 503 "Could not pin the workflow definition" and deletes the spool. Idempotency keys are not read on the audio branch. The image branch reads the `Idempotency-Key` header (proxy.py:25831-25833). | proxy.py:25114-25121 | unpinned | unknown |
| audio-50 | 11 | Any other error from `media_jobs.create` gives 500 `api_error` "Could not create audio media job: <ExcType>", and the spool entry is deleted. | proxy.py:25122-25132 | unpinned | unknown |
| audio-51 | 9 | A `queued` event is appended to the job's events (status queued, progress 0); an append error is swallowed. | proxy.py:25135-25147 | s7_aj_* goldens (`media_job_events`) | unknown |
| audio-52 | 9 | The job is scheduled as a background task (`_schedule_audio_media_job`) and the reply is 202 with `Location`, using the client payload: `object`, `id`, `poll_url`, `content_url` (`/gateway/v1/media/jobs/<id>/content`). | proxy.py:24934-24958, 23555, 23540-23595 (`_image_media_job_response`) | s7_aj_* (202 cells) | unknown |
| audio-53 | 9 | The job row starts with status `queued`, `progress` 0, and the request's `request_id`; the `job_json` record carries the fields listed in the sync spike diff under `media_jobs`. | gateway/media_jobs.py:877 (`create`) | audio_generate_job.json (`job_envelope`, `definition_blob` pinned as present) | differs: spike shows the row-path sync call writes media-job rows and blobs that the golden says must be absent, and `job_json.metadata.mode` and `app_profile` differ |

Counted R2 items: 21 (audio-33 to audio-53).

Note on audio-38: the sync route and the job route disagree for a null model. The job route gives 404, the sync route uses the default. Neither is pinned.

---

## R3: `GET /gateway/v1/media/jobs/{job_id}` (get_media_job)

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| audio-54 | 9 | Looking up a job id that does not exist gives 404 `media_job_not_found`, and the same reply is given to a caller who may not see the job. | gateway/api.py:473 (`job_not_found`), api.py:776-795 | test_gateway_media_api.py (code string); s7_vc_*/s7_ve_* cancel cells | unknown |
| audio-55 | 9 | The job id is not validated before the store lookup; `media_jobs.get` returns `None` for any unknown string. | gateway/media_jobs.py:1419 | unpinned | unknown |
| audio-56 | 2 | Visibility uses the record authorizer (`owns_record("media_job", ...)` → `_job_readable`) and the scoped-record helper; a job the caller cannot read is reported as not found. | gateway/api.py:426 (`scoped_record`), gateway/record_ownership.py:35-38 | test_media_job_identity.py (file exists; audio coverage not checked) | unknown |

Verified detail: for a no-intent submit, `owns_record("media_job", {}, ...)` takes the `media_job` branch and calls `_job_readable`. Whether it returns False for an empty record was not checked.

Counted R3 items: 3.

---

## R4: `GET /gateway/v1/media/jobs` (list_media_jobs)

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| audio-57 | 9 | The list defaults to 50 and is capped at 200 items. | gateway/api.py:87-88, 730-775 | unpinned for audio | unknown |
| audio-58 | 2 | The list returns only the jobs the caller can read (record authorizer applied per row). | gateway/api.py:730-775 | unpinned for audio | unknown |

Counted R4 items: 2.

---

## R5: `GET /gateway/v1/media/jobs/{job_id}/content` (gateway_media_job_content)

Handler: proxy.py:26088.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| audio-59 | 2 | Content preflight runs before the lookup. For an audio job the policy denial gives 403 `not_in_access_plan`. | proxy.py:23825-23850, 26099-26101 | s7_aj_policy_refusal.json; test_media_job_errors.py | unknown |
| audio-60 | 9 | A job id that does not exist gives 404 `media_job_not_found` ("media job '<id>' not found"). A job the caller cannot read gives 404 with message "Media job not found." | proxy.py:26106-26115, 26120-26134 | unpinned for audio (`media_job_not_found` in test_gateway_media_api.py) | unknown |
| audio-61 | 7 | Corrected by A3 (2026-11-08) from the old code at 7daa31769. The job keeps the submit's choice: `metadata_mode` is set on the job from the request body's `extensions` (the runner replays `request`). Signed content handles are always `md=none`: `_media_job_asset_items` calls `build_signed_asset_url` with no mode (proxy.py:23793 at 7daa31769), so neither the GET override nor the caller's default reaches the handle. The precedence the inventory first gave (override, job mode, caller default) is not what the old code does. This is a pre-existing gap, not a parity regression; the new path matches the old. | proxy.py:26122-26130 (GET handler), proxy.py:23793 at 7daa31769 (signing) | pinned: `test_audio_61_job_records_the_submit_metadata_choice`, `test_audio_61_content_handle_is_none_for_a_job_submitted_with_none`, `test_audio_61_content_handle_ignores_the_get_override_on_a_none_job`, `test_audio_61_content_handle_ignores_the_caller_default` (test_s7_parity_audio.py) |

Counted R5 items: 3.

---

## R6: `GET /gateway/v1/media/jobs/{job_id}/results/{name}` (get_media_job_result)

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| audio-62 | 8 | Corrected by A3 (2026-11-08) from the old code at 7daa31769. The route answers 404 `media_job_result_not_found` for a missing job, for an owned completed audio job, and for any result it cannot read. That refusal carries no `Cache-Control` header (`refused` in gateway/api.py, lines 800-803). The `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff` headers are set only on a successful read (gateway/api.py:820). The earlier text said every refusal carries `private, no-store`; the old code does not. | gateway/api.py:796-821 | pinned: `test_audio_62_results_route_is_404_for_a_missing_job`, `test_audio_62_results_route_is_404_for_an_owned_completed_job`, `test_audio_62_refusal_for_a_completed_job_carries_no_cache_control` (test_s7_parity_audio.py) |

Counted R6 items: 1.

---

## R7: `GET` and `POST /gateway/v1/media/jobs/{job_id}/events`

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| audio-63 | 9 | `GET` events returns the job's event list (`queued`, `running`, `completed`, `failed`, `canceled`) and it is bounded by the same caller check as GET job. | gateway/api.py:822-877 | unpinned for audio | unknown |
| audio-64 | 9 | `POST` events lets a caller append an event; the audio runner uses the store appender directly, not this route. | gateway/api.py:878-947; gateway/api.py:184-260 (`store_event_appender`) | unpinned | unknown |

Counted R7 items: 2.

---

## R8: `POST /gateway/v1/media/jobs/{job_id}/cancel` (cancel_media_job)

Cancel calls `_cancel_media_job` (proxy.py:23336) through the router option at proxy.py:15760-15780.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| audio-65 | 9 | Cancelling a queued audio job marks it `canceled` with message "Canceled by requester" and returns 200. | gateway/media_jobs.py:1718 (`request_cancel`); gateway/api.py:948-1011 | s7_aj_cancel_queued.json | unknown |
| audio-66 | 9 | Cancelling a finished job returns 409 `media_job_cancel_conflict` with "This job has already finished". | gateway/media_jobs.py:1718; gateway/api.py:948-1011 | s7_aj_cancel_finished.json | unknown |
| audio-67 | 9 | Cancelling a running job sets `metadata.cancel_requested_at` and returns 202. The runner then fails the cancel (audio-84) and settles as `canceled` "Canceled by requester" with no result bundle. | gateway/media_jobs.py:1718; gateway/api.py:948-1011; proxy.py:23985 (`_settle_native_job`) | unpinned for the running case (no golden or test hit) | unknown |
| audio-68 | 9 | The cancel reply is 202 when `cancel_requested` is set and 200 when the state changed at once. | gateway/api.py:948-1011 | unpinned for audio (`test_media_jobs_contract.py` covers the contract generally) | unknown |

Counted R8 items: 4.

---

## R9: Job runner, scheduler and restart (no HTTP route)

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| audio-69 | 11 | A runner that finds no payload fails the job with "Audio media job payload is missing or invalid". | proxy.py:24791-24830 | unpinned | unknown |
| audio-70 | 11 | A caller who is no longer allowed to use the gateway fails the job with "The account that asked for this job can no longer use the gateway." | proxy.py:24791-24830 | unpinned | unknown |
| audio-71 | 4 | The runner calls `proxy_audio_generate(store_full=True, receipt=...)` without `pinned`, so the manifest and graph are read live from the registry and workflow file. | proxy.py:24791-24830 | audio_generate_job.json (`live_reads` asserted in test_adapter_differential.py `_audio_job_golden`) | unknown |
| audio-72 | 11 | A non-2xx result from the executor goes through `_media_job_failure`. Its categories: authorization → "This workflow isn't in your access plan."; timeout → "The backend did not finish this job in time."; upstream → "Media generation failed (upstream HTTP <status>)."; capacity, safety and invalid_input have their own copy. | proxy.py:23638-23700 (`_media_job_failure`, `_JOB_FAILURE_COPY`) | upstream: s7_aj_upstream_failure.json and test_media_job_errors.py; the other categories unpinned | unknown |
| audio-73 | 11 | An executor exception fails the job with "Audio media job executor error: <ExcType>" and a 502 status code recorded on the job. | proxy.py:24791-24830 | unpinned | unknown |
| audio-74 | 11 | A success whose result refused the bundle (too large or not allowed) fails with "The result was larger or other than the gateway allows, so it was not delivered." | proxy.py:24759 (`_fail_refused_result`) | unpinned | unknown |
| audio-75 | 7 | On success the bytes are sniffed for their container (RIFF/WAVE, fLaC, OggS, ID3/MPEG, ftyp; an unknown type becomes `.wav`), checked with `result_bundle.check_assets`, stored as kind `aud` with media type, size and index, and mirrored with `result_bundle.mirror_assets`. | gateway/audio_media_jobs.py (`audio_artifacts_from_result`, `_normalize_suffix`, `store_audio_artifacts`); proxy.py:24791-24830 | audio_generate_job.json; test_result_bundle_audio.py | differs or unknown: the row path has no job-side diff; the sync diff shows asset names `<sha>.bin` and `.wav`, golden `.wav` |
| audio-76 | 9 | A `completed` event carries `phase` completed, `asset_count` and `media_types`, and the job moves to `succeeded` with progress 100. | gateway/audio_media_jobs.py (`complete_audio_job_payload`); gateway/media_jobs.py:1610 | audio_generate_job.json | unknown |
| audio-77 | 10 | At startup, `_recover_media_jobs` calls `_resume_pending_audio_media_jobs` (proxy.py:24961-24992) for unfinished audio jobs with `metadata.mode == async_audio_generation`; other intents are skipped. | proxy.py:24961-24992 | test_audio_media_job_integration.py | unknown |
| audio-78 | 10 | A job with no payload after restart fails "Audio media job payload missing after proxy restart". | proxy.py:24961-24992 | test_audio_media_job_integration.py (file-level; this exact string matched by name in that file) | unknown |
| audio-79 | 10 | With recovery turned off, or with the service not opted in and COMFY disabled, the job fails "The gateway restarted with job recovery turned off". | proxy.py:24961-24992 | test_audio_media_job_integration.py::test_restart_never_replays_started_cancelled_or_disabled_audio_jobs (case `recovery_disabled`) | unknown |
| audio-80 | 10 | A job that had `cancel_requested_at` set fails at restart with "The gateway restarted before cancellation could be confirmed". The job is failed, not `canceled`. | proxy.py:24961-24992 | test_audio_media_job_integration.py (case `cancelled`) | unknown |
| audio-81 | 10 | A job that was `running` fails at restart with "The gateway restarted before completion could be confirmed; this job was not run again". It is never re-run. | proxy.py:24961-24992 | test_audio_media_job_integration.py (case `running`) | unknown |
| audio-82 | 10 | A queued job that passes the checks is rescheduled. | proxy.py:24961-24992 | unpinned | unknown |
| audio-83 | 11 | A crash in the task marks the job failed with the crash code and logs it. | proxy.py:24934-24958 (`_done` callback) | test_media_job_crash.py (file exists; audio coverage not checked) | unknown |
| audio-84 | 9 | A cancel requested while running is honoured when the next status update tries to succeed: `update_status` refuses SUCCEEDED with `CancellationPending`, and `_settle_native_job` sets `canceled`. The engine's success is held until the HTTP cancel returns 202, so the refused SUCCEEDED attempt happens after the cancel. | gateway/media_jobs.py:1610, 1714; proxy.py:23985 | pinned: `test_audio_84_cancel_while_running_wins_over_success` (test_s7_parity_audio.py; a spy on `update_status` records the refused attempt) | unknown |

Counted R9 items: 16 (audio-69 to audio-84). Totals: R1 32, R2 21, R3 3, R4 2, R5 3, R6 1, R7 2, R8 4, R9 16; 84 items in all.

---

## Unpinned items

These have no golden under `tests/fixtures/adapter_differential/` or `tests/fixtures/s7_*`, and no test file hit on the message or code:

- audio-01 (auth refusal on sync), audio-06 (operator availability override 503), audio-09 (metadata-mode body error), audio-10 (`check_request` refusal), audio-11 (sync unknown model, text not pinned on the sync route), audio-12 (null model on sync), audio-16 (other manifest inputs), audio-17 (`upstream_refused` for audio), audio-18 (no `prompt_id` for audio), audio-24 (transport timeout for audio), audio-26 (generic engine exception), audio-27 (peak normalise), audio-29 (metadata rewrite failure).
- audio-33 (no-intent dispatch), audio-35 (no body validation at submit: invalid bodies get 202), audio-36 (job-route auth refusal), audio-38 (null model on the job route, gives "None" and 404), audio-41 (job-route admission refusal), audio-42 (workflow target policy), audio-45 (generated-block fault), audio-46 (id format on the audio route), audio-47 (the "Could not pin" 503), audio-49 (`media_jobs.create` envelope refusal and idempotency handling), audio-50 (`Could not create audio media job`).
- audio-55 (id not validated before lookup), audio-57 and audio-58 (list default, cap and visibility for audio), audio-60 (content 404 texts for audio), audio-61 (content delivery mode), audio-62 (results always 404), audio-63 and audio-64 (events route), audio-67 (cancel while running), audio-68 (202 versus 200 for audio).
- audio-69 and audio-70 (runner payload and caller-gone texts), audio-73 (executor error text), audio-74 (refused result text), audio-82 (rescheduling a queued job after restart).
- Failure copy in audio-72 for the timeout, capacity, safety and invalid-input categories.

Items that are pinned only at file level, not by name in the test: audio-8 (the 400 for an unknown field), audio-78 (payload missing after restart, checked by file only), audio-83 (crash handling).

## Open points I could not settle from the code I read

- The exact text of `adapter.unavailable_message` (audio-25). It comes from the node-graph adapter, and I did not read it.
- The exact text returned when the audio graph service is disabled by override (audio-06). I read `_service_availability_response` (proxy.py:18920-18945), but I did not check which `service_label` the audio path passes.
- Whether `owns_record("media_job", {}, ...)` returns False for a no-intent submit. It goes through `_job_readable`, which I did not read.
- Where job-side `_run_audio_media_job` sets the status code on the job for an executor exception (audio-73). I read the start of the function only.
- The spike diff has no job-route rows. Any statement about the job route's row path is `unknown`.
- Counts in this file come from my reads of the cited lines. Only the line numbers I read directly were checked. The rest are from the earlier pass and may have moved by a few lines.

Scratch helpers ((a local spike file, not committed)) and the worktree ((a local spike file, not committed)) are removed at the end of this run.

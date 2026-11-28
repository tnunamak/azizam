# Route behaviour inventory: images

Worktree: `origin/release/c2d-int` at 7daa31769 (detached, read-only). Line numbers are from that tree.
Category numbers follow the brief: 1 validation, 2 admission, 3 wire options, 4 engine traffic, 5 receipts,
6 History, 7 Library rows, 8 response, 9 job lifecycle, 10 restart and recovery, 11 failure text.

Pins: `golden:<name>` is `tests/fixtures/adapter_differential/<name>.json`; `s7:<cell>` is
`tests/fixtures/adapter_differential/s7_<ig|ie|ij>_<cell>.json`; `test::<name>` is in `tests/test_adapter_differential*.py`.

Row path: `matches` / `differs (how)` / `unknown`. Source: spike runs in (a local spike file, not committed) (image goldens,
14 failed, 6 passed) and (a local spike file, not committed) (s7 ig cells, 8 failed, 4 passed). pytest `-q` does not name
the passing cells, so the passing cells are `unknown`.

Note: the brief names `gateway/row_routes.py`. It does not exist in this tree.

## 1. images.generate: POST /v1/images/generations (and /openai/v1/images/generations)

Handler: `proxy_image_request` (proxy.py:9098) → `proxy_graph_image_request` (proxy.py:9106). Dispatched from
SelfAuthOp table (proxy.py:34331-34353) and `registry/protocol_frontends.yml:103-120, 895-935`.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| images-01 | 2 | Admission: a disabled image service returns 503 "Image generation not enabled" (type service_unavailable), with Retry-After 30 from the resource-admission path. | proxy.py:9123, 4893 | unpinned | unknown |
| images-02 | 1 | Body that is not valid JSON returns 400 "Invalid JSON body" (invalid_request_error). | proxy.py:9139 | unpinned | unknown |
| images-03 | 1 | A JSON body that is not an object returns 400 "Request body must be a JSON object". | proxy.py:9146 | unpinned | unknown |
| images-04 | 1 | Unknown body fields are refused (no golden cell). | proxy.py:9150-9210 (unknown-field check) | unpinned | unknown |
| images-05 | 1 | `size`, `background`, `n`, `response_format` are validated before the engine is contacted. n above 10 logs a warning and is clamped (`MAX_IMAGES_PER_REQUEST = 10`). | proxy.py:8670, 8688-9097 | unpinned | unknown |
| images-06 | 1 | Missing or empty `prompt` returns 400 "prompt is required" (invalid_request_error). | proxy.py:9216, 9700 | golden:image_prompt_without_id is unrelated; no pin for validation | unknown |
| images-07 | 2 | Route policy is checked after validation and before admission; a denial returns 403 with `access_error`, code `not_in_access_plan`, `details.side` and `details.plans`. | proxy.py:21155 (`_apply_image_route_policy`), 21219 (`_policy_denied_response`) | s7:ig_policy_refusal (test::test_s7_cell[ig-policy_refusal]) | differs: `$.media_jobs.retention_fact_destinations` absent on row path |
| images-08 | 2 | Admission refusal (service busy or capacity) returns the admission response, with its Retry-After. | proxy.py:4799-4893, dispatch admission `_dispatch_admission_response` | s7:ig_admission_refusal | differs: `$.caller[0].response.body.text` differs |
| images-09 | 3 | `model` resolves to an image workflow via the registry; the resolved workflow and manifest are written to the receipt. | proxy.py:9106 onward; `gateway/extension_catalog.py:24-27` | unpinned for the workflow choice on the sync route | unknown |
| images-10 | 3 | Sync generation with `response_format=b64_json` returns `data[].b64_json`. | proxy.py:8519-8600 (`_build_image_response_payload`) | golden:image_sync_b64; test::test_image_sync_b64 | differs: `$.assets[0].json.origin` absent (the Library asset origin is not written) |
| images-11 | 3 | Sync generation with the default `response_format` (url) returns `data[].url` built from the asset URL. | proxy.py:8519-8600 | golden:image_sync_url; test::test_image_sync_url | differs: `KeyError: 'url'` on the row path |
| images-12 | 3 | `n` > 1 runs rounds until `n` images are collected; rounds run at most `images_total + 2`. | proxy.py:8688-9097 (`_generate_images_with_adaptive_batching`) | golden:image_sync_batch; test::test_image_sync_batch | differs: failed assertion (row path returns different image count) |
| images-13 | 3 | `output_format`, `background` and `quality` are echoed in the payload; `quality` is omitted when `auto`. | proxy.py:8519-8600 | unpinned | unknown |
| images-14 | 3 | `size` is echoed as the effective size; a changed size is reported by `X-Image-Size-Adjustment` style metadata on the receipt (`image_size_adjustment`). | proxy.py:6579-6600 (`_image_size_adjustment_header`, `_record_image_size_adjustment`), 9379 | unpinned | unknown |
| images-15 | 3 | If the workflow cannot run in the requested size, the adjustment is recorded as `"<requested>-><effective>"`. | proxy.py:6579-6586 | unpinned | unknown |
| images-16 | 3 | `background` removal: when a workflow needs a fallback, the proxy recurses to background removal and reports `X-Image-Background-Removal` and `X-Image-Background-Removal-Source`. | proxy.py:9067-9071 | unpinned | unknown |
| images-17 | 4 | Engine traffic per round: POST `/prompt`, then poll `/history/{prompt_id}` and `/queue`, then GET `/view` for each output. | proxy.py:8394-8530 (`_graph_run_round`) | golden:image_sync_b64 | differs: `gateway_sleeps` and engine request order (see images-22) |
| images-18 | 4 | Poll sleeps are 0, 0.05, 0.1, 0.2, 0.4, 0.5 and then 0.5 s; the budget is DERIVED.COMFY_TIMEOUT (default 300 s; the tests use 2 s). | proxy.py:8394-8530, config | golden:image_sync_slow_completion | differs: `$.gateway_sleeps[3388].after_engine_requests` |
| images-19 | 4 | OOM during the run calls `/free` to unload models, sets `X-ComfyUI-Models-Freed: true` and retries the round. | proxy.py:8688-9097, 8870 (header), 22774 (receipt metadata) | golden:image_sync_oom_then_complete; test::test_image_sync_oom_then_complete | unknown: test not in the FAILED list nor the passed list shown |
| images-20 | 4 | Rounds stop after a round returns no images. A round that returns no images at round ≥ 2 is a 502. | proxy.py:8939, 21704 | golden:image_sync_batch (pass path); unpinned for the 502 | unknown |
| images-21 | 4 | Generation exceeds the poll budget: 504 with code `image_generation_timeout` and `queue_position`. | proxy.py:8268-8286, 8517 | golden:image_generation_timeout; test::test_image_generation_timeout | differs: `$.gateway_sleeps[3268].after_engine_requests` |
| images-22 | 4 | `_graph_run_round` engine failures: submit timeout → 504; unreachable → 502 (`unavailable_message`); connection drop → 502 "Image backend connection dropped"; OOM at submit → 502 `upstream_out_of_memory` (or `upstream_refused`); no `prompt_id` → 502 "No prompt_id returned". | proxy.py:8303-8330 (`_graph_failure_response`), 8456, 8463, 8470 | golden:image_comfy_unreachable, image_prompt_http_500, image_prompt_without_id, image_execution_error | differs for all four: `$.caller[0].response.body.text` (see images-30) |
| images-23 | 4 | Engine execution error (failed status, status.detail) returns 502. | proxy.py:8394-8530 | golden:image_execution_error | differs: `$.caller[0].response.body.text` |
| images-24 | 4 | History transient errors are retried inside the poll loop. | proxy.py:8394-8530 | golden:image_history_transient_errors; test::test_image_history_transient_errors | differs: `$.gateway_sleeps[3185].after_engine_requests` |
| images-25 | 4 | `/view` returning missing output fails the round (no image). | proxy.py:8394-8530, 8939 | golden:image_view_missing; test::test_image_view_missing | differs: `$.caller[0].response.body.text`; pytest "Failed: …" |
| images-26 | 4 | Client disconnect while polling: the proxy calls cancel (`/api/jobs/{id}/cancel` and `/queue`), and returns 499 with code `image_client_disconnected`. | proxy.py:8288-8298, 8486, 8247 (`_graph_cancel`) | golden:image_client_disconnect_during_poll; test::test_image_client_disconnect_during_poll | differs: `$.gateway_sleeps[3156].after_engine_requests` |
| images-27 | 4 | Task cancellation during a request: the engine is sent a cancel request first (`$.engine_requests[0].body.kind` is the first difference). | proxy.py:8247 | golden:image_request_task_cancelled; test::test_image_request_task_cancelled | differs: `$.engine_requests[0].body.kind` |
| images-28 | 4 | Possible health probe before submit (`/system_stats`); NOT verified in this pass. | not located | unpinned | unknown |
| images-29 | 5 | The receipt opens at route start (`start_image_route_receipt`, before policy). Intent `image.generate`, path `/v1/images/generations`. | proxy.py:21107-21130 | unpinned for field values | differs in media_jobs retention rows (s7 ig) |
| images-30 | 11 | Failure texts: "Image backend connection dropped", "No prompt_id returned", "{label} returned no images", "No images generated" (500), "{label} request timeout" (504). Engine names appear only through `{label}`. | proxy.py:8456, 8470, 8939, 9042, 8268 | golden:image_* (failure cells) | differs: body text on row path (see images-23) |
| images-31 | 5 | Receipt closes: status ok (2xx), rejected (4xx), error (other), with `X-ComfyUI-Models-Freed` copied to receipt metadata; firehose event `image_route_receipt`. | proxy.py:22762-22780 | s7:ig_* | differs in retention facts (s7 ig validation/policy) |
| images-32 | 5 | Replay with the same idempotency key returns the same result; a different body under the same key is a conflict. | proxy.py (replay path, s7 cells) | s7:ig_replay_same, s7:ig_replay_conflict | differs: `$.assets[0].json.origin` |
| images-33 | 5 | Cancel of a queued request calls cancel with kind; cancel of a finished request is a no-op. | proxy.py:8247 | s7:ig_cancel_queued, s7:ig_cancel_finished | differs: `$.engine_requests[0].body.kind` (queued); `$.assets[0].json.origin` (finished) |
| images-34 | 5 | Wait timeout (sync wait exceeded): the request returns the timeout response and the receipt is closed. | proxy.py:8517, 8303 | s7:ig_wait_timeout | differs: `$.gateway_sleeps[3732].after_engine_requests` |
| images-35 | 6 | Kept History summary fields for a generation (workflow, title, capability, asset_id). | `ADMIN`/history writer (not read in this pass) | unpinned | unknown |
| images-36 | 7 | Library asset row for each image: media type image/png (or as returned), origin `{request_id, takes_text, workflow_name}`. | proxy.py:8519-8600, `result_bundle` | golden:image_sync_b64 (`$.assets[0].json.origin`) | differs: origin absent on row path |
| images-37 | 8 | Response headers: `X-Image-Rounds`, `X-Image-Returned`, `X-Proxy-Warning` (joined warnings), `X-Image-Background-Removal`, `X-Image-Background-Removal-Source`, `X-ComfyUI-Models-Freed`. Plus `x-request-id`. | proxy.py:9061-9071, 8870, 22013-22014 | unpinned for the header set | unknown |
| images-38 | 9 | Sync route keeps no job row; it leaves no media-job row, only the receipt and retention facts. | proxy.py:21107 | s7:ig cells (`media_jobs.retention_fact_destinations`) | differs (s7 ig validation/policy) |

## 2. images.edit: POST /v1/images/edits

Handler: `proxy_graph_image_edit_request` (proxy.py:9488). Workflow choice: `_select_edit_workflow` (proxy.py:22122).
Identity alias: `_identity_edit_alias` (proxy.py:9445).

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| images-E01 | 2 | Disabled service returns 503 "Image generation not enabled" (service_unavailable). | proxy.py:9505 | unpinned | unknown |
| images-E02 | 1 | Form is read; a bad form returns 400 `invalid_form`. | proxy.py:9488-9520 | unpinned | unknown |
| images-E03 | 1 | `extensions` that is not JSON returns 400 `invalid_json` (param `extensions`). | proxy.py:9488-9540 | unpinned | unknown |
| images-E04 | 1 | `size` must be `auto` or `WIDTHxHEIGHT` with positive integers; otherwise openai_error_response "size must be 'auto' or WIDTHxHEIGHT with positive integers". | proxy.py:9488-9560 | unpinned | unknown |
| images-E05 | 1 | Missing `image` returns 400 "image is required". | proxy.py:9488-9600 | unpinned | unknown |
| images-E06 | 3 | `model` is resolved through `_identity_edit_alias` and `_select_edit_workflow`; an unknown model returns 400 `model_not_found` "Model '…' is not available for image editing.". | proxy.py:9445, 22122 | unpinned | unknown |
| images-E07 | 1 | Dimension limits check the input image; PIL size detection defaults to 1024x1024 when size is not given. | proxy.py:9488-9700 | unpinned | unknown |
| images-E08 | 1 | Missing `prompt` returns 400 "prompt is required". | proxy.py:9700 | unpinned | unknown |
| images-E09 | 1 | Input image not supplied for a workflow that needs it: 400 `missing_required_image`. | proxy.py:9488-9720 | unpinned | unknown |
| images-E10 | 1 | More input images than the workflow accepts: 400 `too_many_images` "workflow … accepts at most N input image(s)". | proxy.py:9488-9740 | unpinned | unknown |
| images-E11 | 1 | Mask required by the workflow and not supplied: 400 `missing_required_parameter`. | proxy.py:9488-9760 | unpinned | unknown |
| images-E12 | 2 | Policy check, then admission; a refusal writes a receipt with status rejected. | proxy.py:21155, 24642 | s7:ie_policy_refusal, s7:ie_admission_refusal | unknown (ie cells not spiked) |
| images-E13 | 1 | Refused image (`_refuse_edit_image`) returns 400 `invalid_mask` or the refusal copy. | proxy.py:9488-9800 | s7:ie_validation_refusal (partial) | unknown |
| images-E14 | 4 | Image upload to the engine: `graph.upload`. Failure returns 502 "Image upload failed: the engine refused the file.". | proxy.py:9488-9900 | golden:image_edit_upload_refused | unknown (golden not in the spike list) |
| images-E15 | 4 | Upload returns no filename: 502 "No filename returned from upload" (api_error). | proxy.py:9837, 9898, 21491 | golden:image_edit_upload_refused (partial) | unknown |
| images-E16 | 4 | Mask upload failure returns 502 `mask_upload_failed`. | proxy.py:9918 | unpinned | unknown |
| images-E17 | 3 | Mask as source alpha: `X-Image-Mask: workflow` with `X-Image-Mask-Source: <mask_mode>`. Post-composite mask: `X-Image-Mask: post-composite`; receipt warning `mask_mode:post_composite`. | proxy.py:9973-9978 | golden:image_edit | unknown |
| images-E18 | 3 | The mask is applied after generation (post-composite) when the workflow cannot use it as source alpha. | proxy.py:9966-9980 | golden:image_edit | unknown |
| images-E19 | 8 | Response headers `X-Image-Mask`, `X-Image-Mask-Source`, `X-Image-Rounds`, `X-Image-Returned`, size-adjustment header. | proxy.py:9966, 9973-9978, 22013 | golden:image_edit | unknown |
| images-E20 | 4 | Edit runs the same round loop as generate (`_generate_images_with_adaptive_batching`). | proxy.py:9966 | golden:image_edit, s7:ie_* | unknown |
| images-E21 | 5 | Receipt carries accepted inputs (`_record_image_input`) and the resolved workflow. | proxy.py:22733 | s7:ie_* | unknown |

## 3. images.variations: POST /v1/images/variations

Handler: `proxy_graph_image_variations_request` (proxy.py:10100). Offset-inpaint tile path: `_proxy_offset_inpaint_tile_request` (proxy.py:21767), `_run_offset_inpaint_tile_role` (proxy.py:21741-21764).

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| images-V01 | 2 | Disabled service returns 503 "Image generation not enabled". | proxy.py:10126 | unpinned | unknown |
| images-V02 | 1 | Bad form returns 400 `invalid_form`. | proxy.py:10100-10140 | unpinned | unknown |
| images-V03 | 1 | Missing image returns 400 "image is required". | proxy.py:10100-10170 | unpinned | unknown |
| images-V04 | 1 | More than one image returns 400 "/v1/images/variations accepts exactly one input image; N were provided" (`too_many_images`). | proxy.py:10184 | unpinned | unknown |
| images-V05 | 3 | The variation prompt is `IMAGE_VARIATIONS_PROMPT`; the workflow is `images.variations.default_workflow` from proxy_config.yml; denoise is an extension. | proxy.py:10100-10250; `proxy_config.yml` | golden:image_variation (success) | unknown |
| images-V06 | 5 | The receipt metadata gains `variations_delegated_workflow`. | proxy.py:10100-10250 | golden:image_variation | unknown |
| images-V07 | 4 | Offset-inpaint seamless tile: 503 "offset-inpaint tile generation requires the Flux2 base and inpaint workflows plus their manifests" when the workflows or manifests are missing. | proxy.py:21846 | unpinned | unknown |
| images-V08 | 8 | Offset-inpaint success headers: `X-Image-Seamless-Tile-Strategy: offset-inpaint`, `X-Image-Seam-Mask-Width`. | proxy.py:21908-21909 | unpinned | unknown |
| images-V09 | 11 | The string "gateway tile composition error" appears only as a classifier substring in the media-job failure mapper (proxy.py:23707, status 502). No emitter was found in this tree. | proxy.py:23707 | unpinned | unknown |
| images-V10 | 3 | Edit-path `image_size_adjustment` is recorded on variations too. | proxy.py:9966 (edit), 10100 onward (variations) | unpinned | unknown |

## 4. Image job route: POST /gateway/v1/media/jobs/image-generations

Handler: `gateway_image_generations_media_job` (proxy.py:25844-26075). Route table: `gateway/api.py` and
`registry/protocol_frontends.yml:745-760`.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| images-J01 | 1 | Body not JSON: 400 "Invalid JSON body" (invalid_request_error). | proxy.py:25872 | unpinned | unknown |
| images-J02 | 1 | JSON not an object: 400 "JSON body must be an object". | proxy.py:25881 | unpinned | unknown |
| images-J03 | 1 | Metadata control check, model normalisation, then ProtocolError. | proxy.py:25844-25920 | unpinned | unknown |
| images-J04 | 2 | Policy, then admission (`_media_job_admission_response`, queue depth, per-app limits). Service disabled → reason "{label} media backend disabled", Retry-After default 30. | proxy.py:24554-24660 | unpinned | unknown |
| images-J05 | 2 | Admission refusal writes a receipt with `resource_decision` and returns `_gateway_refusal(…, "capacity")`. | proxy.py:24642, 24554-24660 | unpinned | unknown |
| images-J06 | 3 | The job's MediaJob is intent `image.generate`, workflow `image.generate.{model}`. Payload is persisted via `_persist_image_media_job_payload` into ephemeral storage (max 256 entries, 64 MiB). Over the limit: busy response (EphemeralMediaPayloadCapacityError). | proxy.py:23299-23336, 25920-25990 | unpinned | unknown |
| images-J07 | 3 | `_safe_image_media_job_metadata` stores mode `async_image_generation`, request_hash, idempotency, requested_model, size (default 1024x1024), n (clamped 1..10), response_format (default url). | proxy.py:23519 | unpinned | unknown |
| images-J08 | 9 | Job id is `imgjob_idem_<40 hex>` with an idempotency key, `imgjob_seeded_` for seeded requests, otherwise the request id. | proxy.py:23489 | unpinned | unknown |
| images-J09 | 9 | Replay of the same idempotency key returns the existing job (receipt.job_id) and does not create a second. | proxy.py:26000-26060 | s7:ij_replay_same | unknown (ij not spiked) |
| images-J10 | 9 | Replay with a different body under the same key is a conflict. | proxy.py:26000-26060 | s7:ij_replay_conflict | unknown |
| images-J11 | 5 | `media_jobs.create` writes the envelope; a not-recorded envelope returns `_pin_not_recorded_response`; any other exception returns 500 "Could not create image media job: {ExcType}". | proxy.py:26028-26040 | unpinned | unknown |
| images-J12 | 8 | 202 with `Location: /gateway/v1/media/jobs/{id}`, `poll_url` and `content_url`. | proxy.py:23559 | unpinned | unknown |
| images-J13 | 5 | A `queued` event is appended, then the job is scheduled. | proxy.py:26060-26075, 24280 | unpinned | unknown |
| images-J14 | 2 | Validation refusal and policy refusal leave no job; receipts close as rejected. | proxy.py:25844-25920 | s7:ij_validation_refusal, s7:ij_policy_refusal | unknown |
| images-J15 | 2 | Admission refusal at job creation: no job row; receipt closes as rejected. | proxy.py:24642 | s7:ij_admission_refusal | unknown |
| images-J16 | 9 | Wait timeout on the job route: the job continues; the caller gets the 202 response. | proxy.py:25844-26075 | s7:ij_wait_timeout | unknown |
| images-J17 | 9 | Cancel of a queued job: cancel_requested then canceled `"Canceled by requester"`. Cancel of a finished job is a no-op. | proxy.py:23336 (`_cancel_media_job`), 23985 | s7:ij_cancel_queued, s7:ij_cancel_finished | unknown |
| images-J18 | 9 | Disconnect of the caller does not cancel the job (`_MEDIA_JOB_CALLER_GONE`). | proxy.py:23878 | s7:ij_disconnect | unknown |
| images-J19 | 9 | Job success: `result_bundle.mirror_assets`, event `succeeded`, payload deleted. | proxy.py:24036-24280 | golden:image_job_succeeds | unknown |
| images-J20 | 9 | Job failure: status failed, `error[:500]`, event `failed`, payload deleted. | proxy.py:23997 | golden:image_job_upstream_failure | unknown |
| images-J21 | 9 | Job failure copy: fixed copy from `_JOB_KNOWN_ERROR_CODES`; categories timeout, capacity, invalid_input, upstream, safety; upstream text "Media generation failed (upstream HTTP {status})."; retryable is status ≥ 500 for upstream, else capacity or timeout. | proxy.py:23638-23688 | golden:image_job_upstream_failure (partial) | unknown |
| images-J22 | 9 | Retry: `_image_media_job_retry_decision` with max attempts 3 (config), base 5 s, max 30 s, Retry-After honoured. Retry events are appended. | proxy.py:23689-23724, 24036-24280 | golden:image_job_oom_then_complete (partial) | unknown |
| images-J23 | 4 | The job runs `proxy_image_request` through `_JsonBodyRequestShim`, so job engine traffic matches the sync path. | proxy.py:24036-24280 | golden:image_job_succeeds | unknown |
| images-J24 | 4 | Job concurrency bound: `IMAGE_MEDIA_JOB_CONCURRENCY` semaphore; queue key `comfyui-media`. | proxy.py:24372, 24386 | unpinned | unknown |
| images-J25 | 9 | GET `/gateway/v1/media/jobs/{id}`: 404 `media_job_not_found`; 200 with `asset_url_ttl_s`. | gateway/api.py:776 | unpinned | unknown |
| images-J26 | 9 | GET `/gateway/v1/media/jobs/{id}/results`: list of results. | gateway/api.py:796 | unpinned | unknown |
| images-J27 | 9 | GET `/gateway/v1/media/jobs/{id}/events`. | gateway/api.py:822 | unpinned | unknown |
| images-J28 | 9 | POST `/gateway/v1/media/jobs/{id}/cancel`: 503 `media_jobs_unavailable`; 400 `invalid_media_job_cancel`; 409 `media_job_cancel_conflict`; 202 if cancel_requested else 200; 404 `media_job_not_found`. | gateway/api.py:948-985 | unpinned | unknown |
| images-J29 | 9 | GET `/gateway/v1/media/jobs/{id}/content`: 404 `media_job_not_found`; 409 `media_job_not_ready` with status; output missing or unavailable → 410 or 503 (Retry-After 5); unsupported intent → 400 `unsupported_media_job_content`; missing assets → 502 `media_job_assets_missing`. | proxy.py:26092-26190 | unpinned | unknown |
| images-J30 | 8 | Content response: `_openai_image_payload_from_media_job` shapes OpenAI-style `data[]`. | proxy.py:23744 | unpinned | unknown |
| images-J31 | 9 | Progress is set to 5 when the job starts running (proxy.py:24036-24280). The job list route for image jobs was NOT verified in this pass. | proxy.py:24036-24280 | unpinned | unknown |

## 5. Restart and recovery (image jobs and receipts)

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| images-R01 | 10 | Startup scan `_recover_media_jobs` resumes image, audio and video jobs; unsupported modes fail with "The gateway restarted; this job cannot be resumed". | proxy.py:23960-23985 | unpinned | unknown |
| images-R02 | 10 | Payload missing after restart: "Image media job payload missing after proxy restart". | proxy.py:24307 | unpinned | unknown |
| images-R03 | 10 | Recovery turned off: "The gateway restarted with job recovery turned off". | proxy.py:24327 | unpinned | unknown |
| images-R04 | 10 | Restart during cancellation: "The gateway restarted before cancellation could be confirmed". | proxy.py:24330 | unpinned | unknown |
| images-R05 | 10 | Restart during the run: "The gateway restarted before completion could be confirmed; this job was not run again". | proxy.py:24333 | unpinned | unknown |
| images-R06 | 10 | Crash mid-run returns `_MEDIA_JOB_CRASHED` "The job stopped unexpectedly; please try again" (internal_error, retryable true). | proxy.py:23865 | unpinned | unknown |
| images-R07 | 10 | Receipts left open by a restart are closed by `close_orphan` (gateway/receipt_store.py:321); the exact sync-request outcome was NOT verified in this pass. | gateway/receipt_store.py:321 | unpinned | unknown |

## 6. Failure text (engine failure kinds, image group)

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| images-F01 | 11 | Engine unreachable on any call: 502 with `unavailable_message` (names the engine via label). | proxy.py:8303-8330 | golden:image_comfy_unreachable | differs: `$.caller[0].response.body.text` |
| images-F02 | 11 | Engine HTTP 500 on `/prompt`: 502 with the engine's status text. | proxy.py:8394-8530 | golden:image_prompt_http_500 | differs: `$.caller[0].response.body.text` |
| images-F03 | 11 | Engine response without `prompt_id`: 502 "No prompt_id returned". | proxy.py:8470 | golden:image_prompt_without_id | differs: `$.caller[0].response.body.text` |
| images-F04 | 11 | Execution error: 502 with status detail. | proxy.py:8394-8530 | golden:image_execution_error | differs: `$.caller[0].response.body.text` |
| images-F05 | 11 | Timeout: 504 `image_generation_timeout`. | proxy.py:8268 | golden:image_generation_timeout | differs: `$.gateway_sleeps[3268]` |
| images-F06 | 11 | Upstream OOM: 502 `upstream_out_of_memory`. | proxy.py:8463 | golden:image_sync_oom_then_complete (success path only) | unknown |
| images-F07 | 11 | Engine view missing: round fails; the caller's text differs on the row path. | proxy.py:8394-8530 | golden:image_view_missing | differs: `$.caller[0].response.body.text` |

## Unpinned items

Items with no golden, no test, and no s7 cell (from the tables above; this is not exhaustive):

- images-01, 02, 03, 04, 05, 06, 07 (the non-s7 validation checks), 09, 13, 14, 15, 16, 28, 35, 37, 38 (partial pin)
- images-E01 to E11, E16, E19 (headers), E20 (partial), E21 (partial)
- images-V01 to V04, V07, V08, V09, V10
- images-J01 to J08, J11 to J13 (job-create details), J24, J25 to J31
- images-R01 to R07 (all restart and recovery items)
- images-F06 (OOM on the job path has no test)
- Structural items not read in this pass: History summary fields (images-35), `/system_stats` probe (images-28) and the job list route (images-J31).

## Confidence and open items

- High: the sync generate and error-text items that the goldens cover; the s7 ig cell results.
- Medium: the header list (images-37, E19, V08), which comes from grep of the `X-Image-*` names, not a full read.
- Not verified: which 4 s7 ig cells passed (pytest `-q` output); which 6 image goldens passed (inferred as the ones not in the FAILED list).
- Not read: History writer, Library row writer (`result_bundle`), job list route in `gateway/api.py`, `_identity_edit_alias` body.

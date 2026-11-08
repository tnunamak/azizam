# Route behaviour inventory: video group

Source: `origin/release/c2d-int` at 7daa31769 (read-only worktree (a local spike file, not committed)). Goldens and tests were read, not executed.
Spike diffs: (a local spike file, not committed) (row path). Row-path notes say what the spike showed; "unknown" means the spike did not cover it.

Category key: 1 validation · 2 admission · 3 wire options · 4 engine traffic · 5 receipts · 6 History · 7 Library rows · 8 response · 9 job lifecycle · 10 restart · 11 failure text.
Categories with no behaviour on a route are marked "n/a" in that route's table.

---

## R1. `videos.create` direct and template path: POST /v1/videos

Entry: `proxy.py:11959` (`proxy_graph_video_create`) → `_proxy_graph_video_create` at `proxy.py:11978`. Engine submit: `_submit_video_graph` at `proxy.py:11733`. Thin handler `videos_create` at `proxy.py:~26969`. Route table `proxy.py:34359`.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-01 | 2 | Video capability is off (no COMFY): 503 `service_unavailable`, message "Video generation not enabled", no engine request. | proxy.py:12012 | golden `s7_vc_admission_refusal` (503, 0 engine requests) | differs: spike returns 200 queued with a job id and `Location: /gateway/v1/media/jobs/<id>`; one engine request made |
| video-02 | 1 | Non-string `prompt` on direct create is refused before the provider is called. | proxy.py (direct validation); | `test_video_handler_coverage_gaps.py::test_direct_create_rejects_non_string_prompt_before_provider` | unknown |
| video-03 | 1 | Validation refusal (golden shape: 400 with error body); receipt is written for the refusal. | proxy.py:11978 ff. | golden `s7_vc_validation_refusal` | row path: differs only by absent `x-request-id` header and absent receipt row (2 differences in spike) |
| video-04 | 1 | Policy refusal (route policy / plan) is returned before any engine request. | proxy.py:11978 ff. | golden `s7_vc_policy_refusal` (0 differences in spike) | row path: matches |
| video-05 | 1 | `Idempotency-Key` is not read on this route; the same request twice makes two `POST /prompt` calls and two video ids. | proxy.py:11733–11958 (no idempotency read); | golden `s7_vc_replay_same`, `s7_vc_replay_conflict` (show two engine prompts) | row path: differs (spike dedupes on replay; 46 and 42 differences). Decision needed: this is a deliberate change, not on the design's golden-difference list for this route |
| video-06 | 3 | Body fields are normalised to the workflow's `_meta` keys (prompt, size, seconds, input reference, etc.); unknown or non-applicable fields follow `STANDARD_VIDEO_CREATE_FIELDS`. | gateway/video_extensions.py:21; proxy.py:12370–12572 | `test_video_create_sdk_shape.py` (3 tests); `test_video_limits.py` (9 tests) | unknown |
| video-07 | 3 | `size` is clamped to the workflow's bounds (not rejected) by `_parse_size_clamped`. | proxy.py:10999 | `test_video_limits.py` (clamp cases) | unknown |
| video-08 | 3 | `seconds` converted to frame length by `_seconds_to_length`, with the workflow's frame grid. | proxy.py:11022 | `test_video_limits.py`, `test_video_ltx25_h3_workflows.py` | unknown |
| video-09 | 3 | Subject selection for SCAIL-2 replace runs only when the workflow has `subject_select`; see R15. | proxy.py:12627 | `test_video_subject.py` | unknown |
| video-10 | 4 | Engine traffic: `POST /prompt` (ComfyUI) once per request, after `_upload_graph_input` for any input reference (`POST /upload/image`). | proxy.py:11733 ff., 12822 | golden `video_lifecycle`, `s7_vc_*` | row path: matches for the single prompt call; differs on the replay call (see video-05) |
| video-11 | 4 | Engine health: when ComfyUI is unreachable the call returns 502 `api_error` "ComfyUI unavailable at <url>" and no job is kept. | proxy.py:11085 (`_graph_video_unavailable_response`) | golden `video_create_unreachable` | row path: differs. Spike returns 200 queued, then the job fails "The job stopped unexpectedly". The message does name the engine URL on main; the row path must keep the failure-text rule |
| video-12 | 4 | Wait and polling: the create call does not wait for the render; the response is `status: queued` immediately. | proxy.py:11733–11958 | golden `video_lifecycle` | row path: differs when `Prefer: wait` is absent (unknown for the spike; not on main's direct route) |
| video-13 | 4 | Engine submit timeout and disconnect: client disconnect during submit leaves the prompt queued on the engine. | proxy.py:11733 ff. | golden `s7_vc_disconnect` (18 differences in spike) | row path: differs (spike) |
| video-14 | 5 | On success a receipt is opened and closed; the receipt has `intent: video.generate`, `job_id: null` for the sync path. | proxy.py:11989 (`media_ingestion.start_receipt`), 11990 (`protocol="openai.video"`) | golden `s7_vc_validation_refusal` (receipt shape), `video_lifecycle` | row path: differs (receipt fields and `job_id` differ; spike diff lists `receipts`) |
| video-15 | 5 | Receipt field writes (accepted inputs, resolved model, workflow name, manifest hash, policy decision, terminal status). Writes done inside `start_receipt`/`finish_response`; exact field list not re-verified in this pass. | media_ingestion (not re-read) | `test_video_job_persistence.py` (16 tests, partial) | unknown |
| video-16 | 5 | Persistence failure of the video record is swallowed; the call still returns 200. | proxy.py:10399–10416 (`_persist_video_record`, exceptions caught) | unpinned | unknown |
| video-17 | 6 | History: a `video.generate.<workflow>` entry is kept with the workflow name. | proxy.py:12148, 12710 (`workflow=f"video.generate.{workflow_name}"`) | `test_request_history_capture.py` (only as a family; video not pinned by name) | unknown |
| video-18 | 7 | Library: the output is delivered to the Library as a video asset with `video_<prompt_id>` as the asset key on success (see R10 and R8 for delivery). | proxy.py:13390 (`media_ingestion.index_video_delivery`) | golden `video_job_succeeds` | unknown |
| video-19 | 8 | Success body: `{id: "video_<prompt_id>", object: "video", model, azizam_resolved_model, status: "queued", progress: 0, ...}`, status 200, no `Location` header. | proxy.py:6508 (`_video_job_envelope`), 10481 (`_openai_video_payload`) | golden `s7_vc_replay_same` (queued body shape), `video_lifecycle` | row path: differs. Spike body is `{id: "<job:N>", object: "video", status: "queued", ...}` with `Location: /gateway/v1/media/jobs/<id>` and no `model`/`azizam_resolved_model` (differs) |
| video-20 | 8 | Response headers: `x-request-id` present on main. | proxy.py (shared response path) | golden `s7_vc_validation_refusal` (`x-request-id`) | row path: differs (absent in spike) |
| video-21 | 9 | Id is `video_<comfy prompt_id>`; a polled GET of that id is the status path (R7). | proxy.py:11041 `_video_id_from_prompt_id`, 10266 `VIDEO_ID_PREFIX` | golden `video_lifecycle` | row path: differs. Spike's status GET on the created id returned 400 "invalid video_id" (`s7_vc_replay_same`, `wait_timeout`, `cancel_finished`) |
| video-22 | 10 | Restart: the record is hydrated from `video_job_records`; the in-memory progress is lost and status falls back to engine history/queue. | proxy.py:10418, 14270 | `test_video_job_persistence.py` | unknown |
| video-23 | 11 | Engine failure after queue: `video_generation_failed` with segment-of-failure fields for chains; for direct create, error text from `_graph_error_message`. | proxy.py:8182, 8201–8210 | golden `video_execution_error` | row path: differs (spike `video_execution_error`, 27 differences) |
| video-24 | 1 | Missing workflow file: 500-class `api_error` "Workflow file missing for <name>". | proxy.py:12170; 10947 `_load_video_workflow_file` | unpinned (no golden) | unknown |
| video-25 | 1 | Invalid workflow graph JSON resolves to None, so the route answers as a missing workflow. | proxy.py:10947 | unpinned | unknown |

Route counts: 25 items.

---

## R2. `videos.create` chain path (builder set): POST /v1/videos with a chain builder

Entry: `proxy.py:12674–12698` (builder dispatch), `_chain_workflow_for` 12683, `_submit_video_chain` 12837–13016, options parse 12742, shots 12778.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-26 | 1 | Chain policy denial: `_chain_policy_denial` is checked before the workflow manifest or file check. Denial code and message come from that function. | proxy.py:12703 | `test_video_extend.py` (57 tests, chain policy subset); `test_video_extensions.py` (6) | unknown |
| video-27 | 1 | Each option failure raises `PlanError`, with code and param: `invalid_value` (non-number, out of bound), `invalid_workflow_extension` (extensions not a mapping), `invalid_shots` (not a list, not object, missing prompt). | proxy.py:12731–12815 | `test_video_extend.py`, `test_video_extend_h3.py` | unknown |
| video-28 | 1 | `prompt is required` (code `invalid_value`, param `prompt`) for a chain with no prompt. | proxy.py:12791 | `test_video_extend.py` | unknown |
| video-29 | 1 | Validation order differs by route: the chain path calls `_submit_video_chain` before `evaluate_and_apply` and skips `_load_video_workflow_file` and manifest checks; the template path does the opposite. | proxy.py:12694–12698, 12858 | `test_video_extend.py` (order cases, partial) | unknown |
| video-30 | 2 | Chain admission and capacity: the chain's segment count is checked against `_video_max_total_seconds` (40 s) and `MIN_SHOT_SECONDS` (LTX 1.0 s; H3 2.0 s). | gateway/video_extend.py:~40–110; gateway/video_extend_h3.py:~124 | `test_video_limits.py`, `test_video_extend.py`, `test_video_extend_h3.py` | unknown |
| video-31 | 3 | Chain options: `context_seconds`, `context_strength`, shots, extensions, `seconds`; defaults `DEFAULT_CONTEXT_SECONDS=1.0`, `DEFAULT_CONTEXT_STRENGTH=0.9`. H3 has no `context_strength` option. | gateway/video_extend.py:~30; gateway/video_extend_h3.py:234 (`H3ChainEngine`) | `test_video_extend.py`, `test_video_extend_h3.py` | unknown |
| video-32 | 4 | One ComfyUI `POST /prompt` for the whole chain (graph built by the builder; R12/R13). | proxy.py:12674 (comment: one request makes one prompt) | `test_video_ltx25_h3_workflows.py` | unknown |
| video-33 | 4 | Chain progress is tracked per unit (`_video_chain_units`), with segment index from `segment_of_node`. | proxy.py:10324, 10460, 10565 | `test_video_extend.py` | unknown |
| video-34 | 9 | Failure reporting names the failing segment via `chain.units` (`segment_of_node`). | proxy.py:8201–8210 | `video_extend.json` golden | row path: unknown |
| video-35 | 8 | Chain response: same envelope as R1 (`status: queued`, id `video_<prompt_id>`). | proxy.py:6508 | `video_extend.json` | row path: differs as R1 video-19 |

Route counts: 10 items.

---

## R3. `videos.create` multipart form: POST /v1/videos as multipart/form-data

Entry: `_read_video_create_form_body` `proxy.py:11428–11492`; error class `_VideoFormParseError`.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-36 | 1 | Multipart fields are normalised into the same body as JSON; malformed multipart raises `_VideoFormParseError`. | proxy.py:11428–11492 | `test_video_create_sdk_shape.py` (3 tests) | unknown |
| video-37 | 3 | Input reference file is uploaded by `_upload_video_workflow_assets` (`POST /upload/image` to ComfyUI). | proxy.py:11706 | `test_video_keyframes.py` (63 tests, partial) | unknown |
| video-38 | 1 | Invalid input reference: `_VideoAssetError` with param; the message is "invalid video_id" for a bad video reference. | proxy.py:11522 | `test_video_handler_coverage_gaps.py` | unknown |

Route counts: 3 items.

---

## R4. `videos.remix`: POST /v1/videos/{id}/remix

Entry: `proxy_graph_video_remix` `proxy.py:13843`; `_submit_video_remix` `proxy.py:13803`; body `_read_video_request_body` 13735.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-39 | 2 | Unset `videos.remix_workflow` answers 501 `video_remix_not_configured` (the shipped config is off). | proxy.py:1313 (docstring), 13843 ff. | `test_video_remix_replace.py::test_shipped_config_leaves_remix_off`; `test_video_list_delete_remix.py::test_unconfigured_returns_stable_501` | unknown |
| video-40 | 1 | Missing prompt: refused with 400 and a gateway input error. | proxy.py:13878, 13791 | `test_video_remix_replace.py::test_remix_requires_prompt` | unknown |
| video-41 | 2 | Non-owner of the source id gets 404, not a remix error. | proxy.py:13843 ff. (owner check) | `test_video_list_delete_remix.py::test_non_owner_gets_404_not_remix_error` | unknown |
| video-42 | 2 | Admission refusal (service unavailable) → 503. | proxy.py:13867 | unpinned | unknown |
| video-43 | 4 | Remix runs as a new prompt on the engine with the source video's asset as input. | proxy.py:13803 ff. | `test_video_remix_replace.py` (21 tests, stand-in workflow) | unknown |
| video-44 | 9 | Remix creates a new video id `video_<prompt_id>`, same envelope as R1. | proxy.py:11041 | `test_video_remix_replace.py` | unknown |
| video-45 | 1 | Unknown source id: 404. | proxy.py (`_video_job_access_allowed`) | `test_video_list_delete_remix.py::test_unknown_id_is_404` | unknown |

Route counts: 7 items. Remix has no golden; the only engine-facing success is not pinned by a golden.

---

## R5. `videos.edit`: POST /v1/videos/edits

Entry: `proxy_graph_video_edit` `proxy.py:13908`; shares remix submit path. Source video given as `video.id` in the body.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-46 | 2 | Unset remix workflow: 501 `video_remix_not_configured`; every request that passes field checks ends here. | proxy.py:13908; test comment `test_s7_ve` at tests/test_adapter_differential_s7.py:174–175 | golden `s7_ve_success` and `s7_ve_upstream_failure` both record 501 | row path: differs (spike used a configured remix and got 2 assets vs 0 golden). The ve success golden does not pin a real success |
| video-47 | 1 | Missing `prompt` or `video.id`: 400 validation refusal. | proxy.py:13908 ff. | golden `s7_ve_validation_refusal` (5 differences in spike) | row path: differs (spike) |
| video-48 | 2 | Policy refusal is 0 differences in spike. | proxy.py:13908 ff. | golden `s7_ve_policy_refusal` | row path: matches |
| video-49 | 2 | Admission refusal (route not admitting) yields 503. | proxy.py:13867 | golden `s7_ve_admission_refusal` (17 differences) | row path: differs (spike) |
| video-50 | 4 | Cancel while queued and cancel after finish are both recorded goldens. These exercise the media-job cancel path with a None job id (404); video cancel itself is DELETE only. | tests/test_adapter_differential_s7.py:~205–240 | goldens `s7_ve_cancel_queued`, `s7_ve_cancel_finished` | row path: differs (45 and 44 differences) |
| video-51 | 4 | Engine wait timeout and disconnect for edit. | proxy.py:13908 ff. | goldens `s7_ve_wait_timeout` (23), `s7_ve_disconnect` (3) | row path: differs (spike) |
| video-52 | 9 | Replay of the same edit makes repeated engine calls; no idempotency. | proxy.py:13908 ff. | goldens `s7_ve_replay_same` (36), `s7_ve_replay_conflict` (31) | row path: differs (spike) |

Route counts: 7 items. This route has no successful golden; the success behaviour is unpinned.

---

## R6. `videos.extend`: POST /v1/videos/extensions

Entry: `proxy_graph_video_extend` `proxy.py:13018`; receipt start `proxy.py:13028`; chain submit `_submit_video_chain` 12837.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-53 | 1 | Extension validation: the same `PlanError` family as R2 (`invalid_workflow_extension`, `invalid_value`). | proxy.py:12742–12815 | `test_video_extensions.py` (6 tests); `test_video_extend.py` | unknown |
| video-54 | 1 | Source video must be owned by the caller: non-owner refused. | proxy.py:13018 ff. | `test_video_list_delete_remix.py`, `test_video_owner_keys_off_loop.py` (1) | unknown |
| video-55 | 4 | Extension is a chain over a source video (`SourceVideo`) using `build_chain_graph` with `has_source` (LTX: `plan_segments`; H3: `check_source` requires 24 fps ±0.5). | gateway/video_extend.py:124; gateway/video_extend_h3.py:248 | `test_video_extend_h3.py` (49); `test_video_extend.py` (57) | unknown |
| video-56 | 4 | Golden `video_extend` covers one successful extend lifecycle (engine prompt, polling, content). | tests/fixtures/adapter_differential/video_extend.json | golden `video_extend` | row path: unknown |
| video-57 | 1 | H3 extension refused unless the source is 24 fps (±0.5); the refusal names the frame rate. | gateway/video_extend_h3.py:248 | `test_video_extend_h3.py` | unknown |
| video-58 | 9 | Response envelope as R1 (`status: queued`, id `video_<prompt_id>`). | proxy.py:6508 | `video_extend.json` | row path: differs (R1 video-19) |

Route counts: 6 items.

---

## R7. Job status: GET /v1/videos/{id}

Entry: `proxy_graph_video_status` `proxy.py:13102`. Maps internal status with `_OPENAI_VIDEO_STATUS_BY_INTERNAL` (proxy.py:1340).

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-59 | 9 | Internal `running` maps to OpenAI `in_progress`; history `success` maps to `completed`. | proxy.py:1340; 13102 ff. | `test_video_list_delete_remix.py::test_status_maps_internal_running_to_openai_in_progress`, `::test_status_maps_history_success_to_openai_completed` | row path: differs (status GET returns 400 "invalid video_id") |
| video-60 | 9 | Progress is monotonic when history lags live progress. | proxy.py:10286 `_video_progress`, 10457–10530 | `test_video_list_delete_remix.py::test_status_progress_is_monotonic_when_history_lags_live_progress`; `test_video_handler_coverage_gaps.py::test_poll_aliases_are_monotonic_capped_and_complete_once` | unknown |
| video-61 | 9 | Terminal error wins over fallback; fallback never regresses. | proxy.py:13102 ff. | `test_video_handler_coverage_gaps.py::test_status_fallback_never_regresses_and_terminal_error_wins` | unknown |
| video-62 | 9 | Reconciliation: a job the engine no longer reports is reported as orphaned: text `orphaned: <engine label> does not report this prompt_id`. | proxy.py:10752 `_reconcile_video_progress` | `test_video_handler_coverage_gaps.py::test_reconcile_distinguishes_unavailable_active_terminal_and_orphan` | unknown |
| video-63 | 9 | Malformed or wrong-shape engine JSON does not orphan a live video; wrong shape returns "unavailable". | proxy.py:13102 ff. | `test_video_handler_coverage_gaps.py::test_malformed_provider_json_cannot_orphan_live_video`, `::test_wrong_shape_provider_json_is_unavailable` | unknown |
| video-64 | 2 | Non-owner gets 404 (status hidden). | proxy.py:10347–10397 (`_video_job_visible`) | `test_video_list_delete_remix.py::test_status_hidden_from_non_owner`, `::test_status_shown_to_owner` | unknown |
| video-65 | 1 | Malformed id: 400 "invalid video_id". Unknown id: 404. Untracked id under an authenticated key: 404. | proxy.py:11522, 11045 `_prompt_id_from_video_id` | `test_video_list_delete_remix.py::test_invalid_id_is_400`, `::test_unknown_id_is_404`, `::test_untracked_id_is_404_for_an_authenticated_key` | unknown |
| video-66 | 9 | Deleted job is hidden from status (tombstone). | proxy.py:13603 ff. | `test_video_list_delete_remix.py::test_deleted_job_no_longer_shown_to_status_or_content` | unknown |
| video-67 | 9 | Status on a completed job is 200 with `status: completed`, `azizam_status: succeeded`. | proxy.py:10481 | golden `video_job_succeeds`; `s7_vc_wait_timeout` | row path: differs (spike status GET 400) |
| video-68 | 9 | Status on running job: `in_progress`, `azizam_status: running`, progress value. | proxy.py:10481 | golden `s7_vc_wait_timeout`, `s7_vc_cancel_finished` | row path: differs (spike) |
| video-69 | 9 | Status response includes `error` on failed job (text per R1 video-23). | proxy.py:8182 | `video_execution_error` golden | row path: differs (spike) |

Route counts: 11 items.

---

## R8. Content: GET /v1/videos/{id}/content

Entry: `proxy_graph_video_content` `proxy.py:13220`; `_load_completed_video_bytes` 11508; delivery `media_ingestion.index_video_delivery` at 13390.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-70 | 9 | Content of a known unfinished job returns "not ready" (not 200). | proxy.py:13220 ff. | `test_video_list_delete_remix.py::test_content_for_known_unfinished_job_returns_not_ready` | unknown |
| video-71 | 2 | Non-owner gets 404 for content. | proxy.py:10347–10397 | `test_video_list_delete_remix.py::test_content_hidden_from_non_owner` | unknown |
| video-72 | 9 | Cancel during content fetch does not store an asset. | proxy.py:13220 ff. | `test_video_handler_coverage_gaps.py::test_cancel_during_content_fetch_does_not_store_unwanted_asset` | unknown |
| video-73 | 7 | On first successful fetch the file is indexed as a Library asset (`index_video_delivery`). | proxy.py:13390 | golden `video_job_succeeds` (asset row) | unknown |
| video-74 | 8 | Content is served as `video/mp4` bytes from the engine's `GET /view`. Content-type and headers not re-verified in this pass. | proxy.py:13220 ff.; `_load_completed_video_bytes` 11508 | golden `video_job_succeeds` | unknown |
| video-75 | 1 | Asset-error (`_VideoAssetError`) during content read returns a video asset error response. | proxy.py:11494 `_video_asset_error_response` | unpinned | unknown |

Route counts: 6 items.

---

## R9. List: GET /v1/videos

Entry: `proxy_graph_video_list` `proxy.py:13449`; limit default `_VIDEO_LIST_DEFAULT_LIMIT=20` (13445), max 100.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-76 | 1 | Bad `limit`, bad `order`, and bad `after` cursor each refused with 400. | proxy.py:13449 ff. | `test_video_list_delete_remix.py::test_bad_limit_rejected`, `::test_bad_order_rejected`, `::test_bad_after_cursor_rejected` | unknown |
| video-77 | 8 | Default order newest first; `order=asc` supported; `has_more` set when more rows exist. | proxy.py:13449 ff. | `test_video_list_delete_remix.py::test_default_order_is_newest_first`, `::test_order_asc`, `::test_limit_and_has_more` | unknown |
| video-78 | 8 | `after` cursor excludes the cursor row. | proxy.py:13449 ff. | `test_video_list_delete_remix.py::test_after_cursor_excludes_up_to_and_including` | unknown |
| video-79 | 9 | An evicted `after` cursor errors instead of restarting the list. | proxy.py:13449 ff. | `test_video_list_delete_remix.py::test_evicted_after_cursor_errors_instead_of_restarting` | unknown |
| video-80 | 8 | Entry shape includes required fields; failed entries include `error`; running entries not yet in history use the queue. | proxy.py:13449 ff. | `test_video_list_delete_remix.py::test_entry_shape_includes_required_fields`, `::test_failed_entry_includes_error`, `::test_running_entry_not_yet_in_history_uses_queue` | unknown |
| video-81 | 2 | List is scoped to the owner; deleted jobs excluded. | proxy.py:10347–10397 | `test_video_list_delete_remix.py::test_list_scoped_to_owner`, `::test_deleted_job_excluded_from_list` | unknown |
| video-82 | 1 | Only `TransportFailure` is caught; any other exception propagates. | proxy.py:13449 ff. | unpinned | unknown |

Route counts: 7 items.

---

## R10. Delete: DELETE /v1/videos/{id}

Entry: `proxy_graph_video_delete` `proxy.py:13603`. Asset deletion at 13722–13724.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-83 | 9 | Deleting a known terminal job: tombstone (`deleted_at` set), no cancel call. | proxy.py:13603 ff. | `test_video_list_delete_remix.py::test_known_terminal_job_deletes_without_cancelling`; golden `video_delete_while_running` (partial) | unknown |
| video-84 | 9 | Known but not yet in history: still deletable. | proxy.py:13603 ff. | `test_video_list_delete_remix.py::test_known_but_not_yet_in_history_still_deletable` | unknown |
| video-85 | 9 | Queued job is cancelled before tombstoning (cancel call to the engine). | proxy.py:13603 ff. | `test_video_list_delete_remix.py::test_queued_job_is_cancelled_before_tombstoning`; golden `video_delete_while_running` | unknown |
| video-86 | 9 | Cancel failure returns 409 and the job is not deleted. | proxy.py:13603 ff. | `test_video_list_delete_remix.py::test_cancel_failure_returns_409_not_deleted` | unknown |
| video-87 | 9 | Cancel reporting false still tombstones. | proxy.py:13603 ff. | `test_video_list_delete_remix.py::test_cancel_reports_false_still_tombstones` | unknown |
| video-88 | 2 | Only the owner may delete; other credential refused. | proxy.py:13603 ff. | `test_video_list_delete_remix.py::test_owner_cannot_be_deleted_by_a_different_credential`, `::test_owner_can_delete_own_job` | unknown |
| video-89 | 1 | Unknown id 404; invalid id 400. | proxy.py:13603 ff. | `test_video_list_delete_remix.py::test_unknown_id_is_404`, `::test_invalid_id_is_400` | unknown |
| video-90 | 8 | Success body: `{id: "video_…", object: "video", deleted: true}`, status 200. | proxy.py:13722–13728 | unpinned body shape (no direct assertion found) | unknown |
| video-91 | 7 | Delete calls `prompt_memory.delete_asset` for `video_<prompt_id>` and for the record's `asset_id`. Whether this removes the on-disk Library file, or only the prompt row, was not verified in this pass. | proxy.py:13722–13724; mocked in `test_video_handler_coverage_gaps.py:30` | `test_video_handler_coverage_gaps.py` (mock only, does not assert the file) | unknown |
| video-92 | 10 | Tombstone retained briefly then reaped; completed records survive indefinitely; 300 s deleted-record GC. | proxy.py:10677 `_gc_video_job_records_locked` | `test_video_list_delete_remix.py::test_completed_job_survives_indefinitely`, `::test_tombstone_is_retained_briefly_then_reaped`, `::test_completed_job_outlives_the_old_five_minute_terminal_window`, `::test_terminal_progress_gc_no_longer_touches_job_records` | unknown |

Route counts: 10 items.

---

## R11. Media-job video intent: POST /gateway/v1/media/jobs with `video.generate`

Entry: `_VIDEO_MEDIA_JOB_INTENTS` `proxy.py:25131`; validate `_validate_video_media_job_body` 25169; run `_run_video_media_job` 25336–25577; submit `_submit_video_media_job` 25685; dispatch `_dispatch_gateway_media_job_submission` 25803; resume `_resume_pending_video_media_jobs` 25606; policy helpers 25138–25167, 25639, 25656.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-93 | 1 | Video media-job body validated by `_validate_video_media_job_body`; refusal shape is the media-job validation error. | proxy.py:25169 | `test_video_media_job_http.py` (5 tests); `test_video_media_jobs.py` (8) | unknown |
| video-94 | 2 | Route policy for video kind via `DERIVED._ASSET_CONTENT_ROUTE_POLICY_BY_KIND["vid"]`; decision by `_video_job_policy_decision`. | proxy.py:25138–25167, 25639 | `test_video_media_jobs.py`, `test_image_video_route_selectors.py` | unknown |
| video-95 | 2 | Step auth for video job steps: `_video_media_job_step_auth`. | proxy.py:25656 | `test_video_media_job_integration.py` (8 tests) | unknown |
| video-96 | 9 | Accepted job returns 202 with `Location` header and a `vidjob_` id. Polling every 5 s; terminal states follow `result_bundle.mirror_assets`. | proxy.py:25685; `_schedule_video_media_job` 25579 | `test_video_media_job_http.py`; `test_video_media_job_integration.py` | row path: unknown (spike covered the sync `/v1/videos` route, not this) |
| video-97 | 10 | Restart: queued job without upstream id is re-scheduled; running job without upstream id fails with "The gateway restarted before completion could be confirmed; this job was not run again"; cancel requested fails; resume disabled fails. | proxy.py:25606 `_resume_pending_video_media_jobs`; 23960 `_recover_media_jobs` (calls at 23969) | `test_video_media_jobs.py`, `test_video_media_job_integration.py` | unknown |
| video-98 | 9 | Failure marking: `_mark_video_media_job_failed` writes terminal failed state. | proxy.py:25256 | `test_video_media_jobs.py` | unknown |
| video-99 | 4 | Drain counter `_unfinished_openai_video_jobs` (proxy.py:20271) counts unfinished jobs; its docstring says records are in-memory only, which is stale: records are persisted. | proxy.py:20271 | `test_drain_openai_video_jobs.py` | unknown |
| video-100 | 7 | Result bundle is mirrored into Library assets (`mirror_assets`). | proxy.py:25336–25577 | `test_video_media_job_integration.py` | unknown |

Route counts: 8 items.

---

## R12. Graph builder: LTX-2.5 chain, `gateway/video_extend.py`

Entry: `build_chain_graph` `gateway/video_extend.py:182`; `plan_segments` 124; `ChainEngine` 300; `ENGINES` / `engine_for` 351; `segment_of_node` 355. Constants: `DEFAULT_CONTEXT_SECONDS=1.0`, `DEFAULT_CONTEXT_STRENGTH=0.9`, `DEFAULT_MAX_TOTAL_SECONDS=40.0`, `MIN_SHOT_SECONDS=1.0`, `NEGATIVE_DEFAULT`, `filename_prefix="video/ltx-2.5-long"`.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-101 | 3 | Frame grid is 8k+1 (`context_latents`, `first_segment_frames`). | gateway/video_extend.py:102–113 | `test_video_extend.py` | unknown |
| video-102 | 3 | Segments split by `split_blocks` with a max block count; `total_seconds` sums segments and source frames. | gateway/video_extend.py:117–168 | `test_video_extend.py` | unknown |
| video-103 | 3 | Hard-coded LTX-2.5 model files, node classes, sigmas, and `filename_prefix` are in the builder, not data. | gateway/video_extend.py:182–299 | `test_video_ltx25_h3_workflows.py` | unknown |
| video-104 | 4 | Graph is one prompt with continuity: each segment's context latents reference the previous segment's output (`_ref`). | gateway/video_extend.py:178, 182 | `test_video_extend.py` | unknown |
| video-105 | 11 | Failed segment is identified by `segment_of_node` for the error message. | gateway/video_extend.py:355 | `video_extend.json` golden (partial) | unknown |

Route counts: 5 items.

---

## R13. Graph builder: MiniMax H3 chain, `gateway/video_extend_h3.py`

Entry: `build_chain_graph` `gateway/video_extend_h3.py:124`; `H3ChainEngine` 234; `plan_segments` 255; `context_window` 64 (5/22/39/56 frames); `FRAME_STEP=17`, `FRAME_OFFSET=5`, `AUDIO_CONTEXT_FRAMES=24`, `MIN_SHOT_SECONDS=2.0`, `default_negative=""`.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-106 | 1 | Source must be 24 fps ±0.5, else refused by `check_source`. | gateway/video_extend_h3.py:248 | `test_video_extend_h3.py` | unknown |
| video-107 | 3 | Frame grid is 17k+5. | gateway/video_extend_h3.py:64–83 | `test_video_extend_h3.py` (49 tests) | unknown |
| video-108 | 3 | Context window options 5/22/39/56 frames; no `context_strength` option (`option_keys` excludes it). | gateway/video_extend_h3.py:64, 234 | `test_video_extend_h3.py` | unknown |
| video-109 | 3 | Negative prompt default is empty string. | gateway/video_extend_h3.py (`default_negative`) | `test_video_extend_h3.py` | unknown |
| video-110 | 3 | Audio context frames fixed at 24. | gateway/video_extend_h3.py | `test_video_extend_h3.py` | unknown |
| video-111 | 3 | Hard-coded H3 model files and `MiniMaxH3*` node classes in the builder. | gateway/video_extend_h3.py:124–233 | `test_video_ltx25_h3_workflows.py` | unknown |

Route counts: 6 items.

---

## R14. `_meta` reads in `_proxy_graph_video_create`

Entry: `meta = workflow.pop("_meta", {})` at `proxy.py:12368`; reads through 12572.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-112 | 3 | `_meta` is removed from the workflow before submit (`pop`), so it never reaches the engine. | proxy.py:12368 | `test_video_ltx25_h3_workflows.py` | unknown |
| video-113 | 3 | Keys named in `_meta` map prompt, size, seconds, image and related nodes; node ids may be a single id or a list. | proxy.py:12392–12466 | `test_video_ltx25_h3_workflows.py`; `test_video_keyframes.py` | unknown |
| video-114 | 1 | Missing `_meta.image_node` refused as a template error. | proxy.py:12493 | unpinned | unknown |
| video-115 | 3 | `width_node`/`height_node` override the list and single behaviour. | proxy.py:12415 | `test_video_keyframes.py` (partial) | unknown |

Route counts: 4 items.

---

## R15. Subject select (SCAIL-2 replace, SAM 3 nodes)

Entry: `proxy.py:12265` (flag read), `proxy.py:12627` (select); `gateway/video_subject.py` (node ids: `PROBE_NODE="71"`, `FIRST_FRAME_NODE="70"`, `TRACK_NODE="18"`, `COLOR_MASK_NODE="23"`, `SAM_MODEL_NODE="15"`, `POINT_DETECT_NODE="72"`, `REF_RAW_NODE="2"`, `REF_TEXT_NODE="17"`, `REF_DETECT_NODE="19"`, `FIT_CANVAS_NODE="108"`); manifest binding `manifests/workflows/video-scail-2-replace.yml:39, 116`.

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-116 | 3 | `subject_index` is bound by the manifest, not by `apply_point`. | manifests/workflows/video-scail-2-replace.yml:39, 116 | `test_video_subject.py::test_subject_index_is_bound` | unknown |
| video-117 | 1 | Invalid selection refused with 400 naming the param. | gateway/video_subject.py `validate_selection` | `test_video_subject.py::test_invalid_selection_is_a_400_with_the_param` | unknown |
| video-118 | 3 | Point coordinates use the output size. | gateway/video_subject.py `apply_point` | `test_video_subject.py::test_point_uses_the_output_size` | unknown |
| video-119 | 3 | Explicit selection is honoured. | gateway/video_subject.py | `test_video_subject.py::test_explicit_selection` | unknown |
| video-120 | 8 | Warnings from `warnings_for` and tracked count from `tracked_count` reach the response. | gateway/video_subject.py; proxy.py:8190 `_video_job_warnings` | `test_video_subject.py` (partial) | unknown |

Route counts: 5 items. Subject select sits on the create template path (R1).

---

## R16. Id formation, validation, hydration and restart

Entry: `VIDEO_ID_PREFIX` `proxy.py:10266`; `_video_id_from_prompt_id` 11041; `_prompt_id_from_video_id` 11045; `_video_job_records` 10344; `_persist_video_record` 10399; `_hydrate_video_job_records` 10418; startup 14200–14280 (hydrate at 14270); `video_job_records.py` (save strips prompt, `stored_owner_key`; load_all orders by created_at).

| id | cat | behaviour | code | pin | row path |
|---|---|---|---|---|---|
| video-121 | 9 | Id is `video_<comfy prompt_id>`; the prefix is stripped on lookup. | proxy.py:10266, 11041, 11045 | golden `video_lifecycle` | row path: differs (spike ids are `<job:N>`; GET of the created id yields 400 "invalid video_id") |
| video-122 | 1 | Malformed id (not prefix plus id) refused with 400 "invalid video_id". | proxy.py:11522, 11045 | `test_video_list_delete_remix.py::test_invalid_id_is_400` | unknown |
| video-123 | 2 | Owner key is hashed (`owner_key_hash`); access checks `_video_job_access_allowed`, visibility `_video_job_visible`, blocked `_video_job_blocked`. | proxy.py:10347–10397 | `test_video_owner_keys_off_loop.py`; `test_video_list_delete_remix.py` | unknown |
| video-124 | 10 | Records persist in the `video_job_records` SQLite table in `media_jobs.DB_PATH`, saved on change, hydrated at startup. Prompt text is not stored (stripped on save and load). | gateway/video_job_records.py (save, load_all); proxy.py:10402–10421 | `test_video_job_persistence.py` (16 tests) | unknown |
| video-125 | 10 | Migration `video_job_records_v1` is recorded in the schema table. | golden spike diff path `$.media_jobs.video_job_records` | goldens `s7_vc_*` (migration row) | row path: differs (spike `media_jobs.video_job_records` differs in every vc/ve cell) |
| video-126 | 10 | After restart, status falls back to engine history and queue (in-memory progress is lost). | proxy.py:10286, 14270 | `test_video_job_persistence.py` (partial) | unknown |
| video-127 | 9 | Completed records survive restart; a tombstoned record is reaped after its window. | proxy.py:10677 | `test_video_list_delete_remix.py` (see video-92) | unknown |
| video-128 | 1 | Video list and delete refuse ids that are not video ids. | proxy.py:11522 | `test_video_list_delete_remix.py` | unknown |

Route counts: 8 items.

---

## Unpinned items

No golden or test covers these. Each needs a parity test before the row path ships.

1. video-05 idempotency: the direct route ignores `Idempotency-Key`. The row path dedupes. Golden exists (`s7_vc_replay_same`) and pins the old double-submit, so the change must be signed off, not silently taken. (Pinned by the spike golden only; the design's golden-difference list does not include it.)
2. video-16 persistence failure is swallowed and the call still returns 200.
3. video-24 and 25 missing or invalid workflow file answers, and the exact message for an invalid graph.
4. video-42 remix admission refusal (503).
5. video-46 edit success: no golden ever records a real successful edit; the only edit goldens record 501.
6. video-75 asset error during content read.
7. video-82 list propagates non-`TransportFailure` exceptions.
8. video-90 delete success body shape (no direct assertion).
9. video-91 delete: whether the Library file is removed from disk, or only the prompt row; the test mocks `delete_asset` and asserts nothing about files.
10. video-96 and 97 media-job success path and restart behaviour on the row path (spike covered only sync `/v1/videos`).
11. video-114 `_meta.image_node` missing.
12. video-74 content-type and response headers for content bytes.
13. video-15 receipt field list (only partly verified in this pass).
14. video-29 validation-order difference between chain and template routes.
15. video-12 wait behaviour without `Prefer: wait` on the direct route.

## Notes on confidence

- Code locations are from reading at 7daa31769. Line numbers near 12370–12815 and 13018–13900 are exact. Rows marked "approx" or with a range are close but not exact.
- Pins were checked by test name and golden name, not by running the suite.
- Spike results are from `vdiff.txt` and are only as good as the spike's own scenario setup. The `s7_ve_success` baseline (0 assets in golden) is itself suspect, so the ve success comparison is weak evidence.
- Stale comment: `_unfinished_openai_video_jobs` (proxy.py:20271) says records are in-memory. They are persisted (video-99, video-124).

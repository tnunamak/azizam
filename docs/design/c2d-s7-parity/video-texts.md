# Video refusal texts (old code, base commit)

Every refusal the old create, edit, extend, status, content, list and delete routes sent, taken by running the OLD handlers on the base commit through the video rig (`tests/support/video_rig.py`). A new implementation must send the same status, `message`, `type`, `param` and `code`. A dash means the field was absent.

| case | status | type | param | code | message |
|---|---|---|---|---|---|
| invalid_json | 400 | `invalid_request_error` | - | - | Invalid JSON body |
| prompt_number | 400 | `invalid_request_error` | `prompt` | - | prompt must be a string |
| prompt_missing | 400 | `invalid_request_error` | - | - | prompt is required |
| prompt_empty | 400 | `invalid_request_error` | - | - | prompt is required |
| seconds_low | 400 | `invalid_request_error` | - | - | seconds must be between 1.0 and 10.0 for model video-ltx-2.5-t2v |
| seconds_high | 400 | `invalid_request_error` | `seconds` | `video_total_length_exceeded` | total video length would be 9999.0 s, over the 40 s limit (videos.max_total_seconds). Quality drops with each added segment; finish this video or start a new one. |
| seconds_text | 400 | `invalid_request_error` | - | - | seconds must be a number |
| seconds_negative | 400 | `invalid_request_error` | - | - | seconds must be between 1.0 and 10.0 for model video-ltx-2.5-t2v |
| size_number | 500 | - | - | - | - |
| size_bad_text | 200 | - | - | - | - |
| model_unknown | 404 | `invalid_request_error` | `model` | `model_not_found` | Model 'nope-model' is not available for video generation. |
| model_number | 500 | - | - | - | - |
| i2v_without_input_reference | 400 | `invalid_request_error` | `input_reference` | - | Model video-ltx-2.5-i2v is image-to-video and requires input_reference |
| h3_i2v_without_input_reference | 400 | `invalid_request_error` | `input_reference` | - | Model video-minimax-h3-i2v is image-to-video and requires input_reference |
| unknown_field | 400 | `invalid_request_error` | `bogus_field` | `unknown_parameter` | unknown top-level field; move gateway-native controls under extensions.com.azizam.workflow |
| shots_on_t2v | 400 | `invalid_request_error` | `shots[0].seconds` | `invalid_value` | shots[0].seconds must be a number |
| seconds_past_native | 400 | `invalid_request_error` | `seconds` | `video_total_length_exceeded` | total video length would be 60.0 s, over the 40 s limit (videos.max_total_seconds). Quality drops with each added segment; finish this video or start a new one. |
| scail_without_source | 404 | `invalid_request_error` | `model` | `model_not_found` | Model 'scail-2-replace' is not available for video generation. |
| audio_text | 400 | `invalid_request_error` | `audio` | `unknown_parameter` | unknown top-level field; move gateway-native controls under extensions.com.azizam.workflow |
| extensions_text | 400 | `invalid_request_error` | `extensions.com.azizam.prompts.mode` | `invalid_prompt_mode` | extensions must be an object |
| extensions_workflow_unknown | 200 | - | - | - | - |
| multipart_bad | 400 | `invalid_request_error` | - | - | prompt is required |
| input_reference_bad | 400 | `invalid_request_error` | - | - | the input is not a file type this model accepts |
| status_invalid | 400 | `invalid_request_error` | - | - | invalid video_id |
| status_unknown | 404 | `not_found` | - | - | video_id not found |
| content_invalid | 400 | `invalid_request_error` | - | - | invalid video_id |
| content_unknown | 404 | `not_found` | - | - | video_id not found |
| delete_invalid | 400 | `invalid_request_error` | - | - | invalid video_id |
| delete_unknown | 404 | `not_found` | - | - | video_id not found |
| list_limit_zero | 400 | `invalid_request_error` | `limit` | - | limit must be between 1 and 100 |
| list_limit_text | 400 | `invalid_request_error` | `limit` | - | limit must be an integer |
| list_limit_high | 400 | `invalid_request_error` | `limit` | - | limit must be between 1 and 100 |
| list_order | 400 | `invalid_request_error` | `order` | - | order must be 'asc' or 'desc' |
| list_after_malformed | 400 | `invalid_request_error` | `after` | - | invalid after cursor |
| list_after_unknown | 400 | `invalid_request_error` | `after` | `cursor_not_found` | after cursor does not reference a currently known video job (it may have been deleted or evicted) |
| remix_unconfigured | 404 | `not_found` | - | - | video_id not found |
| remix_no_prompt | 404 | `not_found` | - | - | video_id not found |
| edit_no_prompt | 400 | `invalid_request_error` | `prompt` | - | prompt is required |
| edit_no_source | 400 | `invalid_request_error` | `video` | - | video is required: upload a file or pass {"id": "video_..."} |
| edit_unconfigured | 404 | `not_found` | - | - | video_id not found |
| content_not_ready | 404 | `invalid_request_error` | - | `video_not_ready` | Video content is not ready yet (status=queued) |
| content_view_error | 502 | `api_error` | - | - | ComfyUI /view error: 404 |
| extend_seconds_text | 400 | `invalid_request_error` | `seconds` | `invalid_value` | seconds must be a number |
| extend_no_prompt | 400 | `invalid_request_error` | `prompt` | `invalid_value` | prompt is required |
| extend_no_source | 400 | `invalid_request_error` | `video` | - | video is required: pass {"id": "video_..."} or upload a file |
| extend_unknown_source | 404 | `not_found` | - | - | video_id not found |
| extend_bad_shots | 400 | `invalid_request_error` | `shots` | `unsupported_parameter` | shots is not supported on extensions; call extensions once per shot, or send shots to POST /v1/videos |
| extend_bad_workflow_ext | 500 | - | - | - | - |
| disabled | 503 | `service_unavailable` | - | - | Video generation not enabled |

## Notes

- `size_number`, `model_number` and `extend_bad_workflow_ext` answer 500 with no error body on the old code (an unhandled type error). These are old defects, not contract. The parity tests do not pin them; a new path may answer 400.
- `size_bad_text` and `extensions_workflow_unknown` answer 200 on the old code (the bad value is ignored).
- `remix_*` and `edit_unconfigured` answer 404 `video_id not found` because the source id does not exist in the rig; the unconfigured-route text is not reachable without a configured remix workflow.
- `scail_without_source` answers `model_not_found` because the rig has no such model; source-required refusals need the model's manifest and are covered where the manifest ships.
- A text pin may not be weakened by any delta kind.

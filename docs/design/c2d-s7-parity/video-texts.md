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
| model_unknown | 404 | `invalid_request_error` | `model` | `model_not_found` | Model 'nope-model' is not available for video generation. |
| i2v_without_input_reference | 400 | `invalid_request_error` | `input_reference` | - | Model video-ltx-2.5-i2v is image-to-video and requires input_reference |
| h3_i2v_without_input_reference | 400 | `invalid_request_error` | `input_reference` | - | Model video-minimax-h3-i2v is image-to-video and requires input_reference |
| unknown_field | 400 | `invalid_request_error` | `bogus_field` | `unknown_parameter` | unknown top-level field; move gateway-native controls under extensions.com.azizam.workflow |
| shots_on_t2v | 400 | `invalid_request_error` | `shots[0].seconds` | `invalid_value` | shots[0].seconds must be a number |
| seconds_past_native | 400 | `invalid_request_error` | `seconds` | `video_total_length_exceeded` | total video length would be 60.0 s, over the 40 s limit (videos.max_total_seconds). Quality drops with each added segment; finish this video or start a new one. |
| scail_without_source | 404 | `invalid_request_error` | `model` | `model_not_found` | Model 'scail-2-replace' is not available for video generation. |
| audio_text | 400 | `invalid_request_error` | `audio` | `unknown_parameter` | unknown top-level field; move gateway-native controls under extensions.com.azizam.workflow |
| extensions_text | 400 | `invalid_request_error` | `extensions.com.azizam.prompts.mode` | `invalid_prompt_mode` | extensions must be an object |
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
| edit_no_prompt | 400 | `invalid_request_error` | `prompt` | - | prompt is required |
| edit_no_source | 400 | `invalid_request_error` | `video` | - | video is required: upload a file or pass {"id": "video_..."} |
| content_not_ready | 404 | `invalid_request_error` | - | `video_not_ready` | Video content is not ready yet (status=queued) |
| content_view_error | 502 | `api_error` | - | - | ComfyUI /view error: 404 |
| extend_seconds_text | 400 | `invalid_request_error` | `seconds` | `invalid_value` | seconds must be a number |
| extend_no_prompt | 400 | `invalid_request_error` | `prompt` | `invalid_value` | prompt is required |
| extend_no_source | 400 | `invalid_request_error` | `video` | - | video is required: pass {"id": "video_..."} or upload a file |
| extend_unknown_source | 404 | `not_found` | - | - | video_id not found |
| extend_bad_shots | 400 | `invalid_request_error` | `shots` | `unsupported_parameter` | shots is not supported on extensions; call extensions once per shot, or send shots to POST /v1/videos |
| disabled | 503 | `service_unavailable` | - | - | Video generation not enabled |
| remix_unconfigured | 501 | `invalid_request_error` | - | `video_remix_not_configured` | Video remix is not configured on this gateway. Set videos.remix_workflow in proxy_config.yml to enable it. |
| edit_unconfigured | 501 | `invalid_request_error` | - | `video_remix_not_configured` | Video remix is not configured on this gateway. Set videos.remix_workflow in proxy_config.yml to enable it. |
| remix_source_video_extension | 400 | `invalid_request_error` | `extensions.com.azizam.workflow.source_video` | `invalid_workflow_extension` | source_video is set by the remix route; do not pass it as a workflow extension |
| create_unknown_extension_input | 400 | `invalid_request_error` | `nope` | `invalid_workflow_extension` | unknown input |
| long_video_not_supported_shots | 400 | `invalid_request_error` | `shots` | `long_video_not_supported` | Model video-ltx-2.5-t2v cannot render shots or clips longer than 10 s; use video-long, ltx-2.5-long or minimax-h3 |
| invalid_shots_not_json | 400 | `invalid_request_error` | `shots` | `invalid_shots` | shots must be a JSON list of {prompt, seconds} objects |
| invalid_shots_empty | 400 | `invalid_request_error` | `shots` | `invalid_shots` | shots must be a list of 1 to 12 {prompt, seconds} objects |
| invalid_shots_not_object | 400 | `invalid_request_error` | `shots[0]` | `invalid_shots` | shots[0] must be an object |
| invalid_shots_no_prompt | 400 | `invalid_request_error` | `shots[0].prompt` | `invalid_shots` | shots[0].prompt is required |
| invalid_shots_no_seconds | 400 | `invalid_request_error` | `shots[0].seconds` | `invalid_value` | shots[0].seconds must be a number |
| long_video_unknown_option | 400 | `invalid_request_error` | `extensions.com.azizam.workflow.zzz` | `invalid_workflow_extension` | unsupported long-video option 'zzz'; supported: state_ledger, context_seconds, context_strength |
| extend_unknown_option | 400 | `invalid_request_error` | `extensions.com.azizam.workflow.zzz` | `invalid_workflow_extension` | unsupported long-video option 'zzz'; supported: state_ledger, context_seconds, context_strength |
| remix_missing_source | 404 | `not_found` | - | - | video_id not found |
| edit_missing_source | 404 | `not_found` | - | - | video_id not found |
| content_no_video_output | 500 | `api_error` | - | - | No video output found in job |
| create_engine_unavailable | 502 | `api_error` | - | - | ComfyUI unavailable at http://comfy.fk.test |

## Notes

- Old defects with two allowed answers. A numeric `model` or `size` answers 500 with no error body on the old code (an unhandled type error); a new path may answer 400 `invalid_request_error`. An unparsable text `size` is ignored on the old code (200, default size); a new path may answer 400 `invalid_request_error`. A non-mapping `extensions.workflow` on extend answers 500 on the old code, and an unknown `extensions.workflow` key on create answers 200. The tests accept exactly these alternatives and never a different status.
- `remix_missing_source` and `edit_missing_source` use a source id that does not exist. The unconfigured-route rows use a source the caller owns, with `videos.remix_workflow` unset, so they reach the 501 branch.
- `scail_without_source` answers `model_not_found` because the rig has no such model. `long_video_not_supported_shots` is reached by configuring a model with no chain workflow. `content_no_video_output` is a finished prompt whose outputs hold no video file. `create_engine_unavailable` is a create with the engine unreachable.
- A text pin may not be weakened by any delta kind.

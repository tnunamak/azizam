---
title: Error codes
summary: The gateway's error shapes, and the next step for each code.
---

# Error codes

Use the HTTP status and `code` in program logic. Show `message` to people. The gateway uses three error shapes, depending on the route.

## Error shapes

**Browser routes.** The routes that the browser interface uses, under `/me/api`, return a `details.layer` that says where the request stopped. See [Troubleshooting](../operate/troubleshooting.md#find-where-the-request-stopped).

```json
{
  "error": {
    "code": "not_authenticated",
    "message": "You're signed out. Sign in again.",
    "details": {"layer": "account"}
  }
}
```

**Gateway API routes.** The routes under `/gateway/v1` return a code and a message:

```json
{"error": {"code": "media_job_not_found", "message": "media job 'job_123' not found"}}
```

**OpenAI-compatible routes,** and gateway-wide refusals such as `request_too_large` and `gateway_draining`, use the OpenAI shape, with `message`, `type`, `param` and `code`. The Anthropic and Ollama routes use their own protocol's shape. See each page under [Build with the API](../api/index.md).

## Any route

| Code | Status | Next step |
| --- | --- | --- |
| `request_too_large` | 413 | Make the request smaller. See [request limits](configuration.md#request-limits). |
| `gateway_draining` | 503 | Wait for the `Retry-After` interval, then try again. |
| `resource_admission_denied` | 503 | The engines are busy. Wait for the `Retry-After` interval, then try again. |

## Media jobs API

| Code | Status | Next step |
| --- | --- | --- |
| `media_job_not_found` | 404 | Check the job ID. Only your app's jobs are visible. |
| `media_job_not_ready` | 409 | Wait until the job has succeeded, then read its content. |
| `unsupported_media_job` | 400 | Use a supported `intent`. See [Media jobs](../api/media-jobs.md). |
| `invalid_media_job` | 400 | Fix the request body. |
| `invalid_media_job_event_query` | 400 | Pass `after_sequence` or `after_id`, not both. |
| `media_job_cancel_conflict` | 409 | The job can no longer be canceled. Check its state. |
| `media_jobs_unavailable` | 503 | Try again later. |

The tables below list the browser route codes, by layer.

## Account

| Code | Next step |
| --- | --- |
| `not_authenticated`, `session_user_not_found` | Sign in again. |
| `owner_required` | Choose your own work. |
| `source_not_allowed` | Choose one of your apps. |
| `make_disabled` | Ask the owner to let your account make things. |
| `too_many_jobs` (HTTP 429) | Wait for one of your jobs to finish. |

## Library

| Code | Next step |
| --- | --- |
| `asset_not_found` | Choose another item in Library. |
| `asset_unavailable` | Check a copy you downloaded. |
| `prompt_not_accessible` | Download without the prompt. |
| `job_not_found` | Choose a job from your own list. |
| `job_finished` | Refresh to see the result. |
| `job_finishing` | Wait for the result to appear in Library. |
| `source_pending` | Wait for the source picture to finish. |
| `source_missing`, `source_not_found` | Choose another source picture. |
| `input_file_unavailable` | Send the input file again. |

## Request

| Code | Next step |
| --- | --- |
| `invalid_cursor`, `invalid_limit`, `invalid_since` | Refresh the list. |
| `invalid_status`, `invalid_source`, `invalid_date` | Choose the filter again. |
| `invalid_metadata` | Choose **Download** again. |
| `unsupported_period` | Usage covers the current month only. Refresh the page. |
| `unsupported_intent`, `invalid_prompt_mode` | Refresh the page and try again. |
| `invalid_json`, `unsupported_field` | Refresh the page and try again. |
| `model_required` | Choose a model. |
| `prompt_required` | Enter a prompt. |
| `prompt_too_long` | Shorten the prompt to the limit in the message. |
| `invalid_n` | Choose a picture count in the stated range. |
| `edit_needs_source` | Choose a picture to edit. |
| `generate_takes_no_source` | Remove the source picture. |
| `one_source` | Send one source picture. |
| `idempotency_conflict` | Refresh before you submit again. |
| `input_not_accepted`, `input_set_by_gateway` | Remove the named input. |

## Gateway

| Code | Next step |
| --- | --- |
| `make_unavailable` | Ask the owner to set up picture making. |
| `make_busy` (HTTP 503, `Retry-After: 60`) | Try again in a minute. |
| `make_queue_failed` | Try again. |
| `input_lost`, `restart_before_finish` | Make the item again. |
| `restart_while_canceling` | Make it again if you still want it. |
| `restart_while_running` | Check Library before you make it again. |
| `alias_misconfigured` | Ask the owner to check the model's settings. |

## Engine, model and workflow

| Code | Layer | Next step |
| --- | --- | --- |
| `model_not_deployed` | `engine` | Choose another model. |
| `no_picture` | `model` | Try again. |
| `input_mapping_invalid` | `workflow` | Ask the owner to check the named input's mapping. |
| `make_failed` | `unknown` | Try again. |

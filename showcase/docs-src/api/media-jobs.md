---
title: Media jobs
summary: Submit picture work as a job, check its progress, and download the result.
---

# Media jobs

A job keeps working when your connection drops. Save its `job_id` and check the job. Do not start a new job only because a connection closed.

`POST /gateway/v1/media/jobs` takes an `intent` and a `request`. This page uses `image.generate`, whose `request` is an OpenAI image request. The route also accepts `image.edit`, `audio.generate`, `music.generate`, `sfx.generate` and `video.generate`. Other intents return `unsupported_media_job`.

Set the environment variables in the [API overview](index.md#set-up-your-environment) first, and export `AZIZAM_IMAGE_MODEL` with an image model your key can use.

## Submit a job

Generate an idempotency key once for new work. If the submission response is lost, rerun only the submission pipeline with the same key and unchanged request. Do not rerun the key-generation line for a retry.

```sh
export AZIZAM_IDEMPOTENCY_KEY="$(python3 -c 'import uuid; print(uuid.uuid4())')"
python3 - <<'PY' | curl --fail-with-body -sS "$AZIZAM_BASE_URL/gateway/v1/media/jobs" -H "Authorization: Bearer $AZIZAM_API_KEY" -H "Idempotency-Key: $AZIZAM_IDEMPOTENCY_KEY" -H 'Content-Type: application/json' --output job.json --data-binary @-
import json, os
print(json.dumps({"intent": "image.generate", "request": {"model": os.environ["AZIZAM_IMAGE_MODEL"], "prompt": "A blue ceramic bowl on a plain table.", "n": 1}}))
PY
```

## Check the job

```sh
JOB_ID=$(python3 -c 'import json; print(json.load(open("job.json"))["job_id"])')
curl --fail-with-body -sS "$AZIZAM_BASE_URL/gateway/v1/media/jobs/$JOB_ID" \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
```

Check again every few seconds.

| State | Meaning |
| --- | --- |
| `queued`, `running` | The job is active. |
| `succeeded`, `failed`, `canceled` | The job is finished. |

Treat any other state as not finished.

To cancel, send `POST /gateway/v1/media/jobs/{job_id}/cancel`. HTTP 202 means that the cancel was requested. Keep checking the job: it is not finished until it reaches a finished state.

## Read progress events

```sh
curl --fail-with-body -sS "$AZIZAM_BASE_URL/gateway/v1/media/jobs/$JOB_ID/events" \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
```

Each job numbers its events with its own `sequence`. Keep the highest `sequence` you processed for each job, so that repeated events do not update your interface twice. Do not compare sequence numbers between jobs. An event's payload holds only progress fields, such as `progress`, `step`, `total`, `phase` and `status`.

| Query parameter | Meaning |
| --- | --- |
| `after_sequence` | Return only events after this sequence number. |
| `limit` | Events per page. Default 50, at most 200. |

To read only new events, pass the highest `sequence` you have as `after_sequence`.

## Retry safely

Retry a lost submission response with the same idempotency key and unchanged request. Use a new key for new work. Do not rely on a changed request replaying the old job: the media-job store rejects mismatched or unverifiable owner, operation or input with `idempotency_conflict`. The store retains replay eligibility while a job is active, or while its last update is within 14 days. After that, a repeated submission can start new work.

## Download the result

When the job has succeeded, read its content. Before that, this route returns HTTP 409 with `media_job_not_ready`.

```sh
curl --fail-with-body -sS "$AZIZAM_BASE_URL/gateway/v1/media/jobs/$JOB_ID/content" \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
```

Job results are signed URLs, not base64 data. Anyone with a signed URL can download the file until the URL expires, without a key. Share it as carefully as a key, and do not change its query parameters.

Keep the asset ID if you want to use the result as an input later. Any key of the app that created an asset can reuse it by ID. Other apps get HTTP 403. To get a fresh signed URL for an asset your app owns:

```sh
curl --fail-with-body -sS "$AZIZAM_BASE_URL/gateway/v1/assets/$ASSET_ID" \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
```

Agents can run media workflows with the [MCP](mcp.md) `run` and `job` tools.

# W6b behaviour capture (differential harness)

Branch: `engine/w6b-harness`, from `engine/w6b` at `84fb76e6` (equal to `origin/main`
then). No production code changed: the harness only adds test support, tests and goldens.

The harness records what the gateway does at its edges for 57 scenarios, and compares
that record with a golden committed from the base. It is the proof that a refactor of the
in-tree ComfyUI, speech (TTS) and transcription (STT) clients changed nothing a caller,
an engine, a receipt, a media job or a stored asset can see.

## What a trace holds

One JSON document per scenario, `tests/fixtures/adapter_differential/<name>.json`.

| Key | Content |
|---|---|
| `caller` | Each request the scenario sent to the real app (`method`, path, sorted query, headers, body) with the response: status, headers (all but `date` and `server`), body. |
| `engine_requests` | Every request the gateway sent to an engine, in start order. `step` is the caller call in flight, `to` the host, `outcome` the status or the exception class (`ConnectError`, `ReadTimeout`, ...). Headers are lower-cased and sorted; credentials show as `<present>`; `user-agent` (a library version) is dropped. Query is sorted. A call from a blocking `httpx.Client` is marked `"client": "sync"`. |
| `gateway_sleeps` | Each `asyncio.sleep` the gateway code asked for (delay, caller step, engine requests so far). The sleep itself is skipped, so the poll cadence is data and no scenario waits. |
| `receipts` | Every row of the route receipt store, `receipt_json` parsed. |
| `media_jobs` | Every row of every media job table (jobs, events, hidden assets, prompt rows), in rowid order. |
| `assets` | The asset cache: file name, suffix, size, sha256 (sidecar `.json` files parsed). |
| `notes` | Facts a scenario states itself (for example how many jobs were scheduled). |

Bodies are described exactly. JSON and text keep their characters, so key order and
spacing are part of the record. Binary is its length, sha256 and first eight bytes.
A multipart body is split into parts (header block as sent, body described the same
way); the random boundary is replaced by `<boundary>` in the header and the body.

The fake engines are the scenario-driven fakes in `tests/support/fake_upstream`
(protocols `comfyui` and `openai-compatible`), served in process by host name. A fake that
meets an unexpected request answers 599, and a required exchange that is never called
fails the scenario: the fake contract is checked as well.

## Scenarios

Each is one test; the test name is the golden name.

ComfyUI (`tests/test_adapter_differential.py`, 32): `image_sync_b64`, `image_sync_url`
(and the signed-url download), `image_sync_batch` (n=3), `image_sync_slow_completion`
(poll cadence), `image_job_succeeds` (submit, poll, executor, poll, content, asset,
events), `image_job_upstream_failure`, `image_edit` (image and mask), `image_variation`,
`image_comfy_unreachable`, `image_prompt_http_500`, `image_prompt_without_id`,
`image_execution_error`, `image_generation_timeout` (`/queue` and cancel),
`image_sync_oom_then_complete`, `image_job_oom_then_complete`,
`image_history_transient_errors`, `image_view_missing`, `image_edit_upload_refused`,
`image_request_task_cancelled`, `image_client_disconnect_during_poll`, `video_lifecycle`
(create, pending and finished status, content, list, delete, list), `video_delete_while_running`
(cancel and history delete), `video_execution_error`, `video_remix`, `video_extend`,
`video_job_succeeds`, `video_create_unreachable`, `audio_generate_sync`,
`audio_generate_job`, `audio_generate_unreachable`, `audio_generate_execution_error`,
`audio_generate_with_object_info` (the blocking `/object_info` lookup and the derived UI workflow).

Speech and transcription (`tests/test_adapter_differential_speech.py`, 25): `tts_buffered_wav`,
`tts_buffered_mp3`, `tts_stream_pcm`, `tts_stream_sse`, `tts_sse_from_buffered_engine`,
`tts_upstream_4xx`, `tts_upstream_5xx`, `tts_fallback_to_second_engine`,
`tts_stream_upstream_error`, `tts_timeout`, `tts_unreachable`, `tts_custom_voice_clone`
(voice create, voice-check, reference transcript from the STT engine, speak twice),
`tts_voice_design`, `tts_voice_quality_unavailable`, `stt_json`, `stt_verbose_json`,
`stt_chunked` (75 s recording, three chunks merged), `stt_stream_native_sse`,
`stt_stream_buffered_engine`, `stt_upstream_4xx`, `stt_upstream_5xx`, `stt_timeout`,
`stt_connection_lost`, `stt_unreachable`, `stt_translations`.

## The normaliser

`Normalizer` in `tests/support/adapter_differential.py` replaces volatile values and
nothing else. Ids are numbered by first appearance within one trace and per kind, so
`<req:1>` twice means one id and `<req:1>`, `<req:2>` two ids. Each rule below has a unit
test in `tests/test_adapter_differential_normalizer.py`, and so does what it must leave alone.

| Replaced | By |
|---|---|
| `req_` plus 32 hex (request ids) | `<req:N>` |
| `mj_`, `vidjob_`, `imgjob_`, `audjob_` plus 32 hex (job ids) | `<job:N>` |
| dashed uuid; bare 32 hex (client ids) | `<uuid:N>`; `<hex32:N>` |
| ISO 8601 date-time; HTTP date | `<iso-time>`; `<http-date>` |
| `sig=...` and `expires=...` of a signed url (text and query) | `sig=<sig>`, `expires=<expires>` |
| the temp directory roots of the run | `<tmp>` |
| a number under a time key (`created`, `expires`, `mtime`, `ctime`, `timestamp`, any `*_at`, `*_at_ms`, `*_at_s`, `*_deadline`), in parsed JSON and in JSON text | `"<time>"` (a null stays null) |
| a number under a duration key (`latency_ms`, `duration_ms`, `elapsed_ms`, `total_ms`, `ttfb_ms`, `queue_ms`, `upstream_ms`, `generation_ms`, `first_byte_ms`) | `"<duration>"` |
| `input_hash` of a job (a digest over fields that hold random ids) | `<digest>` |
| `request_hash` unless the job is seeded or idempotency-keyed (then it is a digest of the request; otherwise it is `uuid4().hex * 2` or `secrets.token_hex(32)`) | `<random-hash>` |
| `content-length` when normalisation changed the body's length | `<N after normalisation>` (N is the normalised length) |
| response headers `last-modified`, `expires` | `<http-date>` |

Everything else is compared exactly, including content digests (asset ids), seeds, counts,
sizes, `retry-after`, `expires_in`, status codes, header values and the order of requests,
rows and JSON keys.

Four opt-ins exist, each for a value that is a process or timing artefact, each named at
the call that uses it, none on by default:

* `unordered_json=True` on `rig.call`: sorts the keys of one JSON response. Used for the
  `202` job document (media job submits), which the gateway builds by iterating a set, so
  its key order follows `PYTHONHASHSEED`.
* `Normalizer.unordered_keys`: a list of strings under such a key is compared sorted. Used for
  `accepted_inputs` in the STT receipts (built from a set).
* `sse_audio=True` on `rig.call`: a speech SSE stream is compared as the joined audio of its
  delta events plus every other event in order. The number and size of the delta events follow
  an ffmpeg pipe read, which depends on timing.
* `chunks=True` on `rig.call`: also records the chunk sizes. Used only for two STT streams the
  gateway builds or relays itself.

## Why the traces are deterministic

Found by diffing runs (in one process, across fresh processes, under `-n 4`, with shuffled
test order) and fixed at the cause, not by loosening the compare:

* `asyncio.to_thread` runs inline: a thread hop made the VRAM probe's `/system_stats` call race
  the main flow. `_VRAM_PROBE_INTERVAL_MIN_S` is raised, so the probe samples before and after a
  round and never on its real-time timer.
* `asyncio.sleep` is recorded and skipped (a sleep of 30 s or more, which is a deadline timer,
  really runs; only STT sets that).
* `random` is seeded per run, so the workflow seeds are fixed.
* The route table binds its store paths at import, so a scenario cannot point the routes at a
  file of its own. It uses the process's test stores and empties them first and last. Tables
  with no rows are left out of the dump (which empty tables exist depends on earlier tests).
* The request id, job id and client id are random by design; they are normalised, not patched.
* Every scenario runs twice in one process and the two traces must be equal (`play`).

## Run, compare, regenerate

The hermetic runner clears the environment and cannot write host paths, so
`AZIZAM_DIFFERENTIAL_OUT` and `AZIZAM_UPDATE_GOLDEN` (read by `compare_golden`) work only
when a scenario runs outside the runner. Under the runner the traces leave the sandbox as
the junit report that `--junit-xml-out` copies out: every scenario records its trace as the
suite property `adapter_trace.<name>`.

```
# check this checkout against its goldens
scripts/run-hermetic-pytest.sh -q tests/test_adapter_differential.py tests/test_adapter_differential_speech.py tests/test_adapter_differential_normalizer.py tests/test_adapter_differential_sensitivity.py

# dump the normalised traces of this checkout (also run the check above; a mismatch does not stop the dump)
scripts/adapter-differential-dump /abs/path/before     # in the base checkout
scripts/adapter-differential-dump /abs/path/after      # in the refactored checkout
diff -r /abs/path/before /abs/path/after               # empty means byte-identical traces

# regenerate goldens (review the diff, it is the behaviour change)
scripts/run-hermetic-pytest.sh --junit-xml-out "$HOME/.tmp/traces-$$.xml" -q tests/test_adapter_differential.py tests/test_adapter_differential_speech.py
python3 -m tests.support.adapter_differential extract "$HOME/.tmp/traces-$$.xml" tests/fixtures/adapter_differential
```

The runner archives `HEAD`, so commit before you run. A mismatch fails the scenario with the
path of the first differing value, both values, the first differing character of a body text,
and a unified diff of the whole entry.

The refactor checkout needs the harness files only: `tests/support/adapter_differential*.py`,
`tests/test_adapter_differential*.py`, `tests/fixtures/adapter_differential/`,
`scripts/adapter-differential-dump`. Its ceilings line in `tests/budgets.toml` carries the
`+107` pytest items (`pytest = 11784`).

## Coupling to the gateway

The rig reaches into these names; a refactor that moves them needs the matching one-line
change in the harness (never in a golden): `proxy.app`, `proxy.COMFY_HOST`, `COMFY_ENABLED`,
`COMFY_TIMEOUT`, `IMAGE_CACHE_DIR`, `IMAGE_MEDIA_JOB_PAYLOAD_DIR`, `_image_url_signing_secret`,
`_service_availability_response`, `_VRAM_PROBE_INTERVAL_MIN_S`, `_video_job_records`,
`_video_progress`, `_video_workflows`, `_schedule_{image,audio,video}_media_job`,
`_run_{image,audio,video}_media_job`, `config`, `gateway.media_jobs.DB_PATH`,
`gateway.receipt_store.DB_PATH`, `gateway.stt_chunking._disk_free_bytes`,
`gateway.custom_voices.secrets`, and the `/object_info` lookup module (found under either
`gateway.comfy_object_info` or `gateway.engine_protocol.node_info`).
`tests/test_adapter_differential_sensitivity.py` names a few more (`_build_comfy_submit_payload`,
`configured_downstream_headers`, `comfy_free_vram_bytes`, `encode_image_bytes`,
`finish_image_route_receipt`, `store_image_bytes`).

## Not captured

* The ComfyUI WebSocket progress listener (`_comfy_ws_listener`): it needs a WebSocket server
  and runs from application start-up, not from a request.
* The image `background: "auto"` decision: it asks a chat model, outside the ComfyUI client.
  Every scenario sends an explicit background; the variation route has no such field, so that
  scenario replaces the decision with "no cut-out".
* Timeouts are injected for speech and transcription as the transport error the client would
  see (`httpx.ReadTimeout`), because the in-process transport has no socket timeouts; the
  client's real timeout wiring is not exercised. The ComfyUI generation timeout is real (a
  2 s budget counted in skipped sleeps).
* `/v1/audio/voice-check` is served by a handler, not a scenario fake, because that path is not
  part of the `openai-compatible` protocol and `fake_upstream` was left alone.
* Streaming cut points of an ffmpeg-piped speech stream (see `sse_audio`), and the engine
  start path of managed speech services (the scenarios run always-on services, so `ensure_target`
  and health probes are not called).
* Video edit with an uploaded clip, the STT diarize route, multi-file uploads, the file-first
  model peek, and media metadata mode `full` (the access plan keeps provenance off, so stored
  bytes carry no per-request data).
* A real client disconnect: `http.disconnect` is delivered to the app, but the gateway's
  middleware stack hides it from `request.is_disconnected()` on the poll path, and the
  scenario pins that. A cancelled request task is pinned separately.
* Goldens hold bytes produced by this machine's ffmpeg (mp3 and resampled audio, chunk
  wavs, the test clips) and the locked Pillow. They are comparable between checkouts on one
  machine, not across machines.

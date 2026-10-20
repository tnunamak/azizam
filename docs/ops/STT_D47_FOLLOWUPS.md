# D47 STT resource followups

All five cross-vendor review findings on `1732c458` are fixed on `fix/stt-d47-followups`.
The checks use real ffmpeg and fake transcription responses. No inference or live services were used.

| Finding | Change | Deterministic evidence |
| --- | --- | --- |
| 1. Full source PCM refuses long stereo recordings | Project source PCM bytes before decoding. If the decode cap or disk reservation cannot hold it, trim each segment from the original source by sample index, without container seeking. | A simulated two-hour 48 kHz stereo upload produces 15 segments without a full source decode. Six real-media cases compare every segment byte against shared PCM for VBR MP3, 22.05 kHz mono MP3 and Opus, under decode-cap and disk constraints. The fixtures end off the planning sample grid. |
| 2. Maximum disk claims refuse short clips | Reserve the upload bound first, then resize the lease after probing. Reserve planning PCM and two segment buffers; shared source PCM is optional. Voice references reserve their own upload size. Bounded decode trials reserve more space before following understated metadata. | On a simulated host with 2 GiB free, a short clip and voice reference are admitted. A large upload is refused at top-up before decode. Further tests prove single credit for written bytes, rollback after refused top-up, and reservation before each decode trial. |
| 3. Voice errors use unknown codes | Use `invalid_value` and `stt_request_deadline`, with `param="voice"`, and preserve the parameter in the speech response. | Both codes belong to the client-error catalogue. The tests assert status, code and parameter. |
| 4. Voice lease contention looks permanent | Return `503 resource_admission_denied` with busy text and `Retry-After: 5` when the shared queue is full. | A speech request with both STT slots occupied receives the retryable response. Existing deadline/cancellation cases verify lease release. |
| 5. Shared PCM lives until owner close | Release extraction PCM in the chunk executor's `finally`, before its caller receives the outcome. | The file is absent while the outer run owner remains open, for both chunked success and a re-plan that returns single-segment passthrough. |

The added normalization probe also preserves previously accepted high-rate input: a failing 96 kHz source test was added before correcting format admission to apply after normalization. The resulting WAV is 16 kHz mono with all expected frames.

## Timing and peak disk

These runs use seeded noise MP3s generated under `~/.tmp` with `libmp3lame -q:a 4`. Each fixture is identical across the main/before/first-fixed comparison. A second run measures the fixed implementation with its resource lease. MP3 generation is excluded from elapsed time. Peak disk is the sum of logical file sizes in the request workspace, including the input, sampled every 10 ms; it can miss shorter peaks. MB means 1,000,000 bytes.

Settings are target 480 seconds, threshold and hard maximum 600 seconds, search width 10 seconds, and 16 allowed segments for the two-hour cases or 64 for four hours. Media/request timeouts are 3,600 seconds. Other resource caps use service defaults, including the 1 GiB decoded-file cap.

| Noise MP3 | Main seconds / peak MB | Before seconds / peak MB | After seconds / peak MB | After segments |
| --- | ---: | ---: | ---: | ---: |
| 2 h, 44.1 kHz mono | 25.5 / 295.7 | 8.1 / 930.4 | 7.9 / 930.8 | 15 |
| 2 h, 48 kHz stereo | 36.6 / 306.6 | refused at 8.1 / 1,380.3 | 36.6 / 306.6 | 15 |
| 4 h, 48 kHz stereo | 160.9 / 613.2 | refused at 13.5 / 1,686.2 | 140.8 / 613.2 | 30 |

Main is `aaea9507a3b94438f6076717d000766feaaacf3a`; before is `1732c45811149b20e466b62ba73f5f633d2269cc`. The after measurement is `0dcb76ec306f2cff79dac52d2e0abddd6a45ed98`; subsequent production changes only preserve high-rate WAV normalization. Peak after reservations were 1,017.7 MB, 537.0 MB and 843.6 MB respectively. The mono speed improvement retains a larger optional shared PCM; limited disk selects source extraction instead. Both stereo refusals were `SttResourceLimitExceeded` before any segment was posted. All generated audio and benchmark workspaces were deleted.

Reproduce from a clean tree:

```sh
./venv/bin/python scripts/benchmark-stt-workspace.py aaea9507 1732c458 HEAD
```

## Verification

The first regression commit was run before implementation and failed all 11 collected cases. The final requested hermetic suite passed **1,352 tests and 175 subtests in 101.60 seconds**. The final off-grid regression fixtures passed **17 tests in 13.51 seconds**. Full hermetic collection measured **15,289 cases**, and `tests/budgets.toml` records that ceiling, up from 15,272. `git diff --check` passed.

The requested suite included `tests/test_stt*.py`, `test_custom_voice_speech.py`, `test_proxy_audio_handler_gaps.py`, `test_engine_protocol_adapter.py`, every `*receipt*.py`, `test_retention_temp_hardening.py`, `test_private_tmp.py`, and `test_engine_knowledge_boundary.py`. The client-error checks were `test_no_upstream_text_in_client_errors.py`, `test_secret_review_r8_client.py`, `test_secret_review_r9_client_egress.py`, `test_secret_review_r10_composed_messages.py` and `test_secret_review_r12_independent.py`. All ran through `scripts/run-hermetic-pytest.sh` from committed trees.

An independent dot review found no introduced blocker in the supplied disk-accounting code. Its review did not execute tests; the results above are the local deterministic checks. Confidence is high for these covered preparation, admission and cleanup paths. Recognition quality was not measured.

## Transcription admission statuses

Both `/v1/audio/transcriptions` and `/v1/audio/translations` return `413` when
an upload exceeds a configured upload, decoded-size, duration, segment-count,
sample-rate or channel cap. Upload parsing uses `stt_upload_limit`; media caps
use `stt_resource_limit`.

Temporary disk shortages and a full shared concurrency gate return
`503 resource_admission_denied`, busy wording and `Retry-After: 5`. The gate
raises storage or concurrency exceptions for every caller, including voice
reference transcription. Receipts retain `capacity_insufficient` with resource
class `disk`, or `queue_full` with resource class `queue`. Disk headroom checks
also retain their denial evidence. OpenAPI describes these statuses and the
retry header for both audio routes.

### Admission verification

Before implementation, the new regression matrix reproduced eight failures;
the two upload-cap cases already passed. The final hermetic run from committed
head `b4307bdf` passed **1,293 tests and 203 subtests in 134.23 seconds**.
It included all `tests/test_stt*.py`, `test_custom_voice_speech.py`,
`test_proxy_audio_handler_gaps.py`, all `*receipt*.py`, the five client-error
files named above, `test_engine_knowledge_boundary.py`, and
`test_openapi_media_schema_honesty.py`.

The matrix checks upload and media cap refusals on both audio routes, disk
shortages at 900 and 1024 MiB free with a 512 MiB lease held, concurrency
exhaustion, retry headers, and receipt reasons. Voice checks also cover both
disk shortages and concurrency evidence. Existing replay-dispatch regressions
now assert the retryable refusal and disk receipt reason.

Full hermetic collection measured **15,315 cases**; `tests/budgets.toml` records
that ceiling, up from 15,300. `git diff --check` passed. No inference, live
gateway or GPU was used. Confidence is high for the covered admission paths.

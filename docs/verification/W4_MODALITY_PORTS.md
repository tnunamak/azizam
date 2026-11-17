# W4 modality execution inventory

Current status: 3D and audio generation are ported. Audio uses declared execution and post-publication delivery; its old polling/storage/completion bodies are deleted. The stopped-attempt sections below are historical evidence. Other modalities remain as inventoried.

Generated from the AST with `python3 scripts/research/modality_paths.py`; completed ports update their execution seams and parity annotations.
The script extracts calls and side-effect call names without importing the app. The explicit dispatch seams document
registry-selected methods that static call resolution cannot prove. These annotations were checked against the named
runner implementations. A frontend shape is a wire parser/renderer, not evidence of generic engine execution.

| Route | Old execution path | Generic path ready / missing | Old/new parity test |
| --- | --- | --- | --- |
| `/v1/audio/speech` | `proxy.py:audio_speech`; inline_tts.invoke; optional dispatch.Runtime.invoke | No: stream/raw relay and voice/fallback policy | No; existing wire oracle: test_adapter_differential_speech.py |
| `/v1/audio/transcriptions` | `proxy.py:audio_transcriptions`; chunked_stt.invoke | No: chunking, stream/deadline/cleanup ownership | No; existing wire oracle: test_adapter_differential_speech.py |
| `/v1/audio/translations` | `proxy.py:audio_translations`; chunked_stt.invoke | No: same STT lifecycle | No; existing wire oracle: test_adapter_differential_speech.py |
| `/v1/images/generations` | `proxy.py:proxy_graph_image_request`; NodeGraphRunner._rounds; optional _protocol_image_result | Partial: adaptive rounds, postprocessing and legacy response ownership | No; existing wire oracle: test_adapter_differential.py |
| `/v1/images/edits` | `proxy.py:proxy_graph_image_edit_request`; NodeGraphRunner._rounds | Partial: masks, upload/composite and postprocessing | No; existing wire oracle: test_adapter_differential_s7.py |
| `/v1/images/variations` | `proxy.py:proxy_graph_image_variations_request`; NodeGraphRunner._rounds | Partial: fixed prompt/denoise and postprocessing | No; existing wire oracle: test_adapter_differential_s7.py |
| `/gateway/v1/media/jobs/image-generations` | `proxy.py:_run_image_media_job`; workflow core / NodeGraphRunner._rounds | Partial: persisted request, retry and receipt lifecycle | No; existing wire oracle: test_adapter_differential_s7.py |
| `/v1/audio/generate` | `proxy.py:proxy_audio_generate`; _PreparedAudioGeneration.run -> declared_operation_job.run (results -> publication -> deliver) | Yes: ported; storage before publication, delivery after publication | Yes: test_w4_audio_declared.py (transition commits) |
| `/gateway/v1/media/jobs` | `proxy.py:_dispatch_gateway_media_job_submission`; modality workers OR declared_operation_job.run | Partial: shipped intents explicitly excluded by declared resolve | No; existing wire oracle: test_adapter_differential_s7.py |
| `/v1/videos` | `proxy.py:proxy_graph_video_create`; video create hand-off / NodeGraphRunner | Excluded: video create owner | No; existing wire oracle: test_adapter_differential_s7.py |
| `/v1/videos/{video_id}/remix` | `proxy.py:proxy_graph_video_remix`; video source / NodeGraphRunner | Partial: source references, video tracking | No; existing wire oracle: test_adapter_differential_s7.py |
| `/v1/videos/edits` | `proxy.py:proxy_graph_video_edit`; video source / NodeGraphRunner | Partial: source references, video tracking | No; existing wire oracle: test_adapter_differential_s7.py |
| `/v1/videos/extensions` | `proxy.py:proxy_graph_video_extend`; chain executor | No: segment chaining and progress/tracking | No; existing wire oracle: test_adapter_differential_s7.py |
| `/v1/3d/generations` | `gateway/multipart_adapter.py:MultipartRunner.run`; exclusive multipart runner | Yes: immediate typed bundle; preserve pool lease and Library provenance | Yes: test_w4_mesh_declared.py (transition commits) |
| `/v1/3d/generations/glb` | `gateway/multipart_adapter.py:MultipartRunner.run`; exclusive multipart runner | Yes: same run, raw response shape | Yes: test_w4_mesh_declared.py (transition commits) |

## Side effects to preserve before each port

| Routes | Side effects and lifecycle |
| --- | --- |
| Speech | Admission, serving lease, voice resolution and fallback, input/format conversion, streaming close ownership, receipt retention, stored audio assets, delivery metadata. InlineTtsAdapter has no start/job support. The optional protocol dispatch calls invoke; it does not make this a declared job. |
| Transcriptions / translations | Alias/policy selection, admission, disk budget, request spool, audio preparation, chunk execution/merge, transforms, deadline/cancel cleanup, SSE relay, attempt metering and receipt input/retention facts. |
| Image generation / editing / variations / jobs | Receipt start/finish, policy, upload and mask/composite handling, adaptive batch/VRAM recovery, generated seeds, result selectors, background removal, metadata strip/store, signed asset URLs, queued request persistence/retries, progress and completion events. NodeGraphRunner uses its own rounds for file-only contracts; calling the generic runner for value contracts does not remove this branch. |
| Audio generation / jobs | Generated defaults, effective duration bounds, graph compilation, submit/history/view, peak normalization, audio metadata stripping, Library store/provenance, response encoding, metering and receipts; persisted jobs retain retries/events. |
| Video generation / remix / edits / extensions | Source authorization and upload, video job tracking, segment chaining, duration limits, generated values, progress, file selection/fetch, signed content and retention receipts. Create hand-offs belong to the separate video workstream. |
| Both 3D routes | Health probe, exclusive-pool observation/lease, retry-after event, egress guard, pinned form and file reads, bounded multipart post, pool release and client close BEFORE Library store, input provenance, cancellation-aware result commit, submit marker and no replay of ambiguous submissions. |

## 3D port

Expect it to work because the old completion stages one named asset and clears the submit marker in the same
transaction. A version-2 Completed(ResultBundle) expresses exactly that transaction. Preparation and exclusive leasing
remain before the generic runner writes the marker; lease release and client close remain before Library storage.
The transition test drives the same route and inputs through both implementations, compares the response including
headers, engine multipart requests and receipts, and covers the recorded mesh scenario matrix. The old loop and its
manual completion transaction are deleted only after this comparison passes. The unchanged S7 recordings remain the
plain wire oracle after deletion.

Live external-engine jobs are unverified: the task forbids touching the live gateway. The hermetic route tests run the
real app, job worker, stores and runner against controlled engine HTTP, including a queued job and content retrieval.
No shipped catalog, OpenAPI pin, parity fixture, budget or reviewed oracle is changed.

## Outcome and remaining work

Both 3D routes now enter declared_operation_job.run. MultipartRunner no longer writes the submit marker or commits the
result itself. The old/new comparison is preserved at commit 6b7626fc7; the final tests read the captured expectations
from tests/fixtures/w4_mesh_wire.json. No other modality was started: speech needs an inline/stream lifecycle contract;
STT needs the chunk/deadline/cleanup contract; image/audio need the graph rounds and postprocessing side effects moved;
video needs tracking/source/chaining and its create hand-offs are owned separately. These are implementation gaps,
not permission blockers. The complete W4 workstream remains unfinished.

The parity harness pins one existing legal scheduling order: asynchronous request capture finishes before the worker
finalizes its handed-off receipt. Otherwise the capture's shared receipt slot can still be unnamed when capture ends,
and the stored retention list varies with scheduling. It waits on the actual shared slot and joins workers, without
masking the retention field. Production receipt scheduling is unchanged; this existing race is not fixed here.

Proposed owner-only budget edit: tests/budgets.toml line 678, change only `pytest = 29830` to `pytest = 29848` (+18 cases).
No reviewed list changes are proposed. Full-suite collection was not run; the owner must reconcile this delta with other lanes.

## Verification

Tests ran only from committed trees through the hermetic runner. Initial red commit c15ac2afc:
`nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-mesh-red.xml -n 4 tests/test_w4_mesh_declared.py`
reported `7 failed in 8.45s` (candidate method absent and default bypassed the declared path).

Transition commit 6b7626fc7, both implementations still present:
`nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-mesh-slot-parity.xml -n 4 tests/test_w4_mesh_declared.py -k old_and_declared -o junit_logging=all`
reported `17 passed in 10.02s`. The log contains no receipt-close errors or ordering timeouts. This is the source of the final wire fixture, including 30 receipt snapshots.

Final code commit a47da8bc6:
```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-mesh-verified.xml -n 4 \
  tests/test_w4_mesh_declared.py tests/test_adapter_differential_s7_mesh_rows.py \
  tests/test_declared_operation_job.py tests/test_engine_knowledge_boundary.py \
  tests/test_implicit_marker_ratchet.py tests/test_c2d_openapi_parity.py \
  tests/test_protocol_traits_from_data.py tests/test_c2d_f_probe_bounds.py
```
reported `324 passed in 23.80s`. This includes queued execution and content retrieval for the 3D routes, the unchanged S7 wire recordings, the OpenAPI pin, both architectural guards, and bounded probe checks. Intermediate receipt-snapshot failures exposed the pre-existing scheduling race described above; no production receipt fix or masked retention field was introduced.

Confidence: 90% for the 3D code port and hermetic wire preservation. Live external-engine behavior is unverified.

## Audio continuation: stopped at publication versus delivery

The duration test commit 7dbdd0ec5 was cherry-picked first (05d95f63d). Its public route tests prove that omitting duration
sends 30 seconds to the graph for both shipped audio workflows while the published request schemas omit the default.
Baseline command:
`nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-audio-default-baseline.xml -n 4 tests/test_audio_duration_publication.py tests/test_audio_route_selectors.py`
reported `9 passed in 9.03s`.

The next step stopped at `media_ingestion.store_audio_response()` followed by `media_delivery.deliver_bytes()`.
The existing store helper calls `index_receipt_assets()`, which creates a succeeded Library job and links it to the
receipt. The old route does this BEFORE stripping for delivery. If stripping raises MediaMetadataError, the response
is 502 (Could not prepare audio for delivery), the receipt fails, and its Library job remains succeeded with one asset.

A declared result callback runs before core's completion transaction. Its receipt is linked to the running job, so the
same `store_audio_response()` saves bytes but `index_receipt_assets()` explicitly skips indexing a linked receipt.
The same stripping failure therefore prevents returning Completed: the job has no published assets or results. These
are different observable publication states even though the input result bytes and helper calls are identical.

`tests/test_audio_declared_result_boundary.py` reproduces the old outcome through the real public route and fake engine,
then drives the real declared runner with the same result bytes and existing storage/stripping helpers. Its adapter
callback is a bounded result-stage probe, not a candidate route implementation. This is not a full old/new route parity
claim. No audio production code or route body was changed, and no half-port was left behind.

Expect the reproduction to hold because it exercises the existing storage/indexing functions against real stores and
injects the failure exactly at the following delivery rewrite. The output was:

- `Old route: HTTP 502, failed receipt, succeeded Library job with one asset.`
- `Declared result callback: rewrite raises before completion; linked job remains running with no assets or results.`

Command:
`nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-audio-boundary.xml -n 4 tests/test_audio_declared_result_boundary.py tests/test_audio_duration_publication.py -o junit_logging=all`
reported `5 passed in 8.60s`.

To continue without changing this case, the declared result path needs a delivery stage AFTER publication (or another
explicit way to preserve a published Library result when delivery fails). The current pre-completion result callback
cannot carry both outcomes by calling the existing helpers unchanged. No change to that completion/delivery contract
was made. Confidence: 95% in the reproduced boundary; the audio port is not implemented.

The continuation adds five tests: four from the duration cherry-pick and one boundary probe. Proposed budget delta is
+5 on top of the accepted 3D delta of +18: at the original tests/budgets.toml line 678, `pytest = 29830` would become
`pytest = 29853` (+23 total), before reconciling other lanes. The owner oracle remains unedited.

## Post-publication delivery implementation and receipt stop

The owner approved a generic post-publication `deliver` hook. `declared_operation_job.run` now accepts an explicit
optional hook or uses the prepared runtime's `deliver` method. It passes the published ResultBundle to that hook and
returns its client answer. A delivery exception propagates without changing the succeeded job or its Library result.
No modality-specific words or branches were added to that module. A terminal replay performs no second delivery.
The existing no-hook callers, including 3D, retain their behavior.

Expect it to work because the versioned runner returns its bundle only after the completion transaction. The two new
hook tests read the real job and bundle inside delivery, then prove both successful delivery and a raised exception
leave publication succeeded. Before implementation they reported `2 failed in 7.76s` (the hook was never called).

The audio candidate separates prepared request/engine state, typed result collection/storage, and post-publication
wire delivery. It calls the existing normalization, storage and metadata functions. At commit 261fb7b50, old/new
comparisons passed for success, delivery failure, engine failure, omitted duration and normalization. They compare
responses, headers, engine requests and stored receipts. The unchanged 30-second pin also passed for both shipped
workflows. Command:

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-audio-port-proved.xml -n 4 \
  tests/test_w4_audio_declared.py tests/test_declared_delivery.py tests/test_declared_operation_job.py \
  tests/test_w4_mesh_declared.py tests/test_audio_duration_publication.py -o junit_logging=all
```

Output: `56 passed in 12.26s`. The wire expectations captured from the old path are in
`tests/fixtures/w4_audio_wire.json`; no existing golden was changed.

The old body was then deleted and the queued worker's duplicate storage/publication removed in commit 91207e14d.
The required checks plus architectural guards, selector tests, publication boundary and both unchanged 3D suites
reported `305 passed in 31.42s` (`w4-audio-port-final.xml`). However the existing queued audio bundle tests exposed a
receipt invariant and triggered the owner's explicit stop condition:

```text
test_the_receipt_carries_the_summary_for_the_operator_only
> assert internal["metadata"]["result_bundle"] == result_bundle.bundle_summary(...)
E KeyError: 'result_bundle'
```

Command:
`nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-audio-bundle-checks.xml -n 4 tests/test_result_bundle_audio.py`
reported `3 failed, 5 passed in 9.27s`. The other two failures were result-limit refusals changing from `result_refused`
to `upstream_error` (per-file limit and publication-total limit). These tests were not changed.

Named receipt invariant: a queued audio job's internal/operator receipt retains its published result-bundle summary;
client and admin projections omit it. `result_bundle.complete_job` links the summary at publication. Moving that before
request completion means `media_ingestion.finish_response` subsequently calls `receipt_store.finalize` with the
in-memory receipt, overwriting the linked metadata. The old queued worker completed its receipt before publishing
and linking the bundle. This ordering change does not affect the five synchronous comparison cases, so their passing
parity did not establish the queued invariant.

Implementation stopped without changing receipt finalization or the invariant. The active audio route and queued
worker were restored to the old implementation; the candidate and its passing old/new comparison remain available as
`proxy_audio_generate_declared` and `tests/test_w4_audio_declared.py`. The generic hook is implemented. Audio is
**parity only**, not ported, and the old body remains. The unsuccessful deletion attempt and failing evidence remain
in history at 91207e14d. No live service, registry, reviewed oracle, budget or existing parity test was changed.

Budget proposal: this turn adds two hook cases and five audio comparison cases (+7). Combined with accepted 3D (+18)
and the duration/boundary continuation (+5), the branch adds +30: original tests/budgets.toml line 678 would change
`pytest = 29830` to `pytest = 29860`, before reconciling other lanes. Confidence: 95% in the generic hook and reproduced
receipt failure; the audio port is not complete.

After restoring the active old audio body, the stopped-state checks ran against commit b803eb504:

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-delivery-stopped-final.xml -n 4 \
  tests/test_w4_audio_declared.py tests/test_declared_delivery.py tests/test_declared_operation_job.py \
  tests/test_audio_declared_result_boundary.py tests/test_audio_duration_publication.py tests/test_audio_route_selectors.py \
  tests/test_w4_mesh_declared.py tests/test_adapter_differential_s7_mesh_rows.py tests/test_result_bundle_audio.py \
  tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py tests/test_c2d_openapi_parity.py
```

Output: `313 passed in 30.90s`. This verifies the safe active route, five old/new synchronous comparisons, the generic
hook, the 30-second pin, both unchanged 3D suites, the existing queued receipt/result invariants, and the architectural
guards. It does not claim the candidate's queued receipt invariant is repaired; implementation stopped at that failure.


## Audio retry completed: receipt merge and typed refusals

The owner authorized both fixes. Request-receipt finalization now keeps the stored publication-owned
`metadata.result_bundle`, even when the terminal request object omits it or carries a stale value. Request-owned
metadata and terminal status still come from finalization. The new focused test first failed with the stored summary
replaced by `{'bundle': 'stale'}` (`1 failed in 9.04s`), then passed with the fix. The existing
`test_the_receipt_carries_the_summary_for_the_operator_only` is unchanged and passes.

Result-limit errors already have a typed carrier: `result_bundle.ResultRefused`, including its `code`. The prepared
path now preserves that exception for the queued executor, which calls the existing `_fail_refused_result` rather
than treating it as an upstream transport error. No generic classifier was widened and no new generic refusal field
was needed. Synchronous requests translate that same type to a 502 result_refused response. The added typed-delivery
case proves the generic hook propagates the exception and its original code while leaving publication succeeded.

The queued executor preserves the old generation-receipt outcome for pre-publication result refusals. A refusal from
post-publication delivery instead fails the request receipt while leaving the published job succeeded. The job's legacy
asset-list bookkeeping runs after successful publication and before rewriting client bytes, so a publication refusal
cannot leave a failed job listing unpublished assets.

Before deletion, the full five-case old/new comparison passed unchanged against the captured wire expectations:
responses, headers, engine requests and receipts for success, delivery failure, engine failure, the private 30-second
default and normalization. The existing functions still perform normalization, storage, stripping and provenance.
At commit 0aa55faa0:

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-audio-retry-parity.xml -n 4   tests/test_w4_audio_declared.py tests/test_declared_delivery.py tests/test_declared_operation_job.py   tests/test_w4_mesh_declared.py tests/test_adapter_differential_s7_mesh_rows.py   tests/test_audio_duration_publication.py tests/test_receipt_store_contract.py -o junit_logging=all
```

Output: `139 passed in 17.11s`. Both audio implementations remain in that commit for review.

The old route's polling/result/delivery body and the queued worker's duplicate artifact storage and bundle completion
were deleted in ebf2fc9be. The request/policy preparation and queued scheduling/receipt lifecycle wrappers remain;
they now call the one declared path. The five comparison cases remain as plain wire tests against the same fixture.
No existing fixture or owner oracle was changed.

Final verification against the committed deletion:

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-audio-retry-final.xml -n 4   tests/test_w4_audio_declared.py tests/test_declared_delivery.py tests/test_declared_operation_job.py   tests/test_audio_declared_result_boundary.py tests/test_audio_duration_publication.py tests/test_audio_route_selectors.py   tests/test_w4_mesh_declared.py tests/test_adapter_differential_s7_mesh_rows.py tests/test_result_bundle_audio.py   tests/test_receipt_store.py tests/test_receipt_store_contract.py tests/test_engine_knowledge_boundary.py   tests/test_implicit_marker_ratchet.py tests/test_c2d_openapi_parity.py -o junit_logging=all
```

Output: `406 passed in 29.77s`. This includes the unchanged queued-audio, 3D and declared-job tests, receipt-store tests,
the boundary where delivery fails after publication, the duration pin, OpenAPI parity and both architectural guards.
No third invariant failed. Live external-engine execution remains unverified; all engine HTTP in these tests is controlled.

Expect it to work because the completion transaction remains the only publication owner, while the finalizer preserves
that owner's linked summary. Existing typed refusals reach the same executor helper as before. Old/new wire comparisons
and the existing queued receipt/result-limit tests verify those ordering assumptions. Confidence: 95% for the tested paths.

This retry adds two cases: finalizer preservation of a stale linked summary and typed delivery refusal. Combined branch
budget proposal at original tests/budgets.toml line 678: `pytest = 29862` (+32 over 29830), before reconciling other lanes.
The budget file, reviewed lists and frozen route graph remain unedited.


## Review follow-ups

Multipart storage preserves a typed `ResultRefused` through the declared runner instead of converting it to an internal
`result_not_kept` error. The adapter-level regression runs the multipart HTTP exchange, injects the exact refusal in
`store_output`, and checks exception identity/code and absence of a published bundle.

The generated audio inventory row now names the declared execution/result/delivery path and its parity test. A test
compares that generated row with the documented row and checks that the prepared run actually calls the declared runner.
These two tests add +2 cases: the combined branch budget proposal is `pytest = 29864` (+34 over 29830), before reconciling
other lanes. The owner budget file remains unchanged.

## Audio failure and publication corrections

The original audio handlers and multipart runner from `fd30cbfca` are frozen as test fixtures. The audio comparison
executes both handlers with identical requests and injected failures. It compares HTTP status/code/message, job status,
job error and typed error fields, and receipt status. It also checks the submission and scrubbed metadata-error logs.
The cases cover submit unreachable/timeout, history unreachable/timeout/upstream error, generation deadline, shutdown,
publication, success, delivery failure, missing output, cancellation at publication, limits and empty asset collections.
The separate empty 3D comparison drives both runners through the raw HTTP route and compares answers, engine requests,
job status and receipt status. Both keep the original HTTP 500 `result_not_kept` refusal. The generic v1 empty-result
message remains unchanged: `The engine returned no result file.`

Audio transport failures are converted at the adapter boundary before generic submission ambiguity handling or poll
retries can alter their wire answers. A generic wait deadline becomes the original HTTP 504 `Audio generation timeout`.
Cancellation pending reaches native settlement; shutdown cancellation leaves the running row for restart recovery.
Synchronous execution rows carry `publication_pending`, so they do not count toward application admission or appear in
listings. Public lookups also hide them. Restart recovery removes interrupted unpublished rows. The existing bundle
transaction accepts optional prepared-run publication fields, so assets, success and public visibility commit together.
Delivery still follows that transaction and cannot undo it.

Synchronous storage checks result limits after provenance preparation and before writing the asset. Empty asset
collections are refused before publication. These two synchronous refusals are the expressly approved tightening:
comparison tests record the old HTTP 200 and require the new HTTP 502, failed receipt and zero stored assets. Queued
refusals retain the original job and receipt outcomes. The queued executor test double now publishes through the
real declared runner, matching the route's publication responsibility instead of bypassing it.

Invalid multipart timeout configuration raises typed `service_unavailable` / `capacity`. Parameterized declared-run
start failures preserve each multipart refusal code and its client message.

Committed-tree hermetic commands and quoted output:

```sh
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-audio-hardening-proof.xml -n 4 tests/test_w4_audio_failure_parity.py tests/test_w4_empty_results.py tests/test_declared_delivery.py tests/test_c2d_f_probe_bounds.py tests/test_declared_operation_job.py
# 88 passed in 9.94s
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-audio-hardening-regression-final.xml -n 4 tests/test_w4_audio_declared.py tests/test_w4_mesh_declared.py tests/test_audio_media_jobs.py tests/test_audio_media_job_integration.py tests/test_audio_job_runs_from_pin.py tests/test_audio_duration_publication.py tests/test_result_bundle_audio.py tests/test_audio_declared_result_boundary.py tests/test_adapter_differential_s7_mesh_rows.py tests/test_modality_paths.py tests/test_audio_route_selectors.py
# 110 passed in 13.27s
nice -n 15 scripts/run-hermetic-pytest.sh --junit-xml-out ../w4-audio-hardening-shared.xml -n 4 tests/test_media_jobs.py tests/test_media_jobs_contract.py tests/test_media_jobs_survivors.py tests/test_media_ingestion.py tests/test_result_bundle.py tests/test_result_bundle_release.py tests/test_receipt_store_contract.py tests/test_engine_knowledge_boundary.py tests/test_implicit_marker_ratchet.py tests/test_engine_protocol_executor.py tests/test_engine_protocol_w4_followups.py
# 467 passed in 22.20s
```

The fixes add 36 cases: 26 audio comparisons, one empty 3D comparison, eight start-refusal parameters and one invalid
timeout parameter. Proposed owner-only budget edit at original `tests/budgets.toml` line 678: `pytest = 29900`, +70 over
29830 including the previously proposed +34. No owner oracle or published parity fixture was edited.

Expect it to work because the frozen original handlers decide the failure answers, and the real completion transaction
publishes asset handles with success. The tests read the job through a separate connection at publication and confirm
that refused synchronous storage writes no asset. Confidence: 97% for the tested local paths; live engines unverified.
No reviewer was started.

# W4 modality execution inventory

Generated from the AST with `python3 scripts/research/modality_paths.py` at the start of the port.
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
| `/v1/audio/generate` | `proxy.py:proxy_audio_generate`; NodeGraphRunner._rounds / legacy submit-history-view loop | Partial: normalize/strip/store and metering | No; existing wire oracle: test_adapter_differential.py |
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

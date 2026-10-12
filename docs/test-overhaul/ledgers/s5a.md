# S5a test-design ledger
Repository: tnunamak/azizam
Pinned source: e35d243907779a8ac19ee8fcef745bb9b32380d7
Assignment: azizam-tso-ledger-s5a-e35d243-r0
Scope: read-only design; 27 allow-listed test files fetched through GitHub, all contents reviewed; 452 test declarations confirmed by static declaration inventory. No source edits, workspace reads, test execution, network backend calls, production reads, Work/Codex tasks or metered tools.

## Result and gate status
The complete scoped design below identifies 25 existing declarations to rewrite in 11 files, with 427 existing declarations retained unchanged and zero deletions, merges or moves. Six additive scenario rows cover stereo and H3 cancellation; A02/A03 are two parameters of one proposed case. "Rewritten (11)" was interpreted as a file count; the brief did not name the eleven files. This is an explicit proposed mapping, not a claim that the author's original eleven-file selection was verified. Retaining all old assertions is mandatory; every changed fixture keeps its old contract, including legacy normalization/passthrough controls where needed.

The strongest concrete gaps are: the SDK envelope test constructs its own payload and uses permissive model_construct; JPEG/WebP tests check format/mode or fully opaque alpha rather than color content; raw PCM ingestion only checks RIFF; and test_video_extend_h3 has no cancellation/receipt-composition case at this SHA. H3 already persists video records through a real media database path, so "replace fake H3 store" alone would misdescribe the current test.

Behavior-preservation gate: UNVERIFIED, not passed. All mutant kills below are static predictions/acceptance requirements, not observed red/green results. Exact production diffs: UNVERIFIED because every production path is outside the read allow-list. Dependency lock/SDK version, external fixture values, production receipt hooks/schema and expanded catalog parameters also remain UNVERIFIED. No unsafe scope expansion was used to fill these gaps. The requested ~28 test-seconds and ~0 saving are planning figures from the brief, not measured here.

## Table conventions
Old IDs are full file::class::test IDs at the pinned SHA; no selected original declaration has a pytest argument parametrization. Internal scenarios in a selected declaration are described explicitly in its invariant/plan. New home is the SAME case ID unless a new A-row is shown; no old case disappears. U=unit; component=real gateway function/HTTP with stubbed external backend; store=real isolated SQLite/cache; HTTP=real app route. Expected messages are proposed deterministic assertion diagnostics, not output from an executed failing test.
P-* means a preservation mutant affecting an old protected invariant. M-* means the named substantive oracle-strengthening mutant; predicted old/new discrimination is stated row by row. Production application and actual kill results for BOTH categories are UNVERIFIED.

## Existing-case preservation ledger
|Row|Old case ID @ line; original parameters|Protected invariant|New home and tier|Named mutant/revert; old vs new prediction|Expected failure message|
|---|---|---|---|---|---|
|R01|tests/test_comfy_workflow_metadata_catalog.py::test_gateway_png_reencodes_keep_comfy_chunks @ L140; no argument params|Plain Pillow resave drops chunks (negative control); offset and mask composition both keep exact prompt/workflow strings.|Same ID; component: real Pillow + gateway transform|P-META predicted kill: missing prompt/workflow chunks; does NOT kill M-RB or M-COMPOSE on uniform/all-edit fixture.|S5A:META prompt/workflow chunks changed; S5A:RGB pixel (x,y) expected RGBA, got RGBA|
|R02|tests/test_image_output.py::TestEncodeImageBytes::test_jpeg_encodes_and_drops_alpha @ L185; no argument params|RGBA input encodes at JPEG quality 80, returns image/jpeg, JPEG container, RGB output (alpha dropped).|Same ID; unit: real Pillow encoder|P-JPEG predicted kill: wrong MIME/format/mode. M-RB predicted survives old format/mode-only assertions; new kills.|S5A:JPEG format/mode/MIME mismatch; S5A:RGB red/blue order differs|
|R03|tests/test_image_output.py::TestEncodeImageBytes::test_webp_encodes_and_keeps_alpha @ L195; no argument params|RGBA input at WebP quality 80 returns image/webp/WEBP and alpha 255 for existing opaque source.|Same ID; unit: real Pillow encoder|P-WEBP predicted kill: wrong MIME/container. M-RB and M-ALPHA predicted survive old opaque-alpha-only oracle; new kills.|S5A:WEBP alpha plane differs; S5A:RGB red/blue order differs|
|R04|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_store_audio_response_stores_indexes_and_wraps_raw_pcm_as_wav @ L171; no argument params|Raw little-endian PCM 01 00 02 00 at 24000 Hz wraps as WAV, stored as aud/.wav under same owner, linked speech.synthesis job/receipt/asset.|Same ID; component: real media/receipt SQLite and asset cache|P-PCM-WRAP predicted kill: invalid RIFF/MIME/asset linkage. M-PCM-SAMPLES predicted survives old RIFF-prefix check; new kills. M-PCM-MONO needs additive stereo case, not this default-mono input.|S5A:PCM expected mono 16-bit 24000 Hz frames=2 samples=[1,2]; S5A:RECEIPT asset/job linkage missing|
|R05|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_build_image_response_payload_indexes_current_delivery_receipt @ L205; no argument params|Current image delivery receipt indexes the produced URL asset in the same request job; image/png and app_image survive.|Same ID; component: real image response producer + SQLite/cache|P-IMAGE-INDEX predicted kill: wrong asset id/MIME/app. M-RB predicted survives old metadata-only test; new kills.|S5A:IMAGE delivery pixels differ; S5A:IMAGE receipt points at a different asset|
|R06|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_public_audio_speech_route_indexes_buffered_response_without_changing_delivered_bytes @ L337; no argument params|Buffered /v1/audio/speech returns upstream WAV unchanged under existing no-op provenance seam, forwards one request without extensions, and stores succeeded receipt/job plus requested/resolved model, request id and audio provenance.|Same ID; HTTP component: real route, stores and provenance; backend transport stub|P-SPEECH predicted kill: delivered bytes changed, headers wrong, extension leakage, receipt/job/provenance fields lost. M-PCM-MONO cannot be killed with old mono-silence fixture; new stereo variant kills if delivery preserves stereo.|S5A:SPEECH delivered bytes changed in passthrough; S5A:PCM channels expected 2 got 1; S5A:PROVENANCE request/model linkage lost|
|R07|tests/test_video_create_sdk_shape.py::VideoCreateMultipartParsingTests::test_input_reference_upload_becomes_data_uri @ L98; no argument params|File multipart input_reference becomes data:image/png;base64 with exact original bytes and closes upload file.|Same ID; unit multipart normalization + decoded fixture|P-UPLOAD predicted kill: wrong URI/MIME/data or unclosed upload. M-UPLOAD-RB predicted killed already by old exact byte identity; decoded variant strengthens validity, not an old-oracle gap for byte changes.|S5A:UPLOAD normalized bytes differ; multipart upload must be closed after normalization|
|R08|tests/test_video_create_sdk_shape.py::VideoCreateResponseEnvelopeTests::test_response_envelope_matches_sdk_video_model @ L159; no argument params|Original fixture yields id=video_abc, object=video, queued, progress 0, seconds string 5 and size 1280x720 as SDK attributes.|Same ID; HTTP contract: real producer + pinned OpenAI SDK + transport stub|P-SDK-FIXTURE is SDK fixture compatibility only, not a production mutant killer. M-SDK-SHAPE predicted survives old isolated handwritten payload and model_construct; new real route/SDK case kills.|S5A:SDK seconds must be a string; S5A:SDK object must be video; S5A:SDK consumer differs from actual producer payload|
|R09|tests/test_video_extend_h3.py::ExtensionRouteTests::test_record_is_tracked_and_persisted_with_chain_info @ L418; no argument params|H3 extension tracks SOURCE_ID lineage and chain segments=1, segment_frames=[187], source_frames=244; persisted video record reload has one segment.|Same ID; HTTP + persistence component: real media/receipt stores|P-H3-CHAIN predicted kill: missing persisted record/chain segment count. M-RECEIPT-CANCEL has no old H3 cancellation case; additive tests are strengthening, not preservation.|S5A:H3 persisted chain differs from response; S5A:RECEIPT extension request/job association missing|
|R10|tests/test_video_media_job_integration.py::VideoMediaJobIntegrationTests::test_executor_polls_video_route_and_stores_rendered_bytes @ L183; no argument params|Submit queued job; replay private prompt; poll running44 then succeeded100; fetch content with index_library=False and METADATA_NONE; persist vid/.mp4 clip.mp4, signed URL, completed event and delete private payload.|Same ID; component: real executor, route producer, stores and backend transport|P-EXECUTOR predicted kill: wrong replay prompt, index_library/metadata mode, storage kind/suffix, status/progress/asset/event or payload cleanup. M-VIDEO-RB expected survives an opaque byte fixture; decoded result kills.|S5A:VIDEO executor lifecycle/asset mismatch; S5A:VIDEO decoded frame (i,x,y) differs; S5A:PAYLOAD remained after completion|
|R11|tests/test_video_workflow_execution.py::VideoWorkflowExecutionTests::test_manifest_video_workflow_runs_against_fake_comfyui @ L64; no argument params|Upload exact input bytes; select rendered output; return Response 200 video/mp4; no _meta submitted; graph source sample.mp4, multiplier=2 and node4.b=2.|Same ID; component: real manifest executor + deterministic HTTP transport|P-WORKFLOW predicted kill: wrong raw response, MIME/status or submitted graph bindings. M-VIDEO-RB predicted survives old opaque rendered-video sentinel; new decoded clip kills.|S5A:WORKFLOW submitted binding differs; S5A:VIDEO downstream bytes/frame oracle differs|
|R12|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_tags_and_lyrics_bind_to_ace_step_inputs @ L548; no argument params|Explicit tags override prompt; lyrics text, both duration nodes=12.0 and seed1234 reach submitted graph; status200.|Same ID; component: real audio route + backend transport fixture|P-ACE-BIND predicted killed by old and new graph assertions; M-PCM-MONO/sample corruption predicted survives old status/graph-only test and killed by stereo decoded variant.|S5A:ACE graph tags/lyrics/duration/seed mismatch; S5A:PCM channels expected 2 got 1|
|R13|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_prompt_falls_back_to_ace_step_tags @ L583; no argument params|Prompt becomes tags and default lyrics remains [instrumental]; status200.|Same ID; component: real audio route + backend transport fixture|P-ACE-BIND predicted killed by old and new graph assertions; M-PCM-MONO/sample corruption predicted survives old status/graph-only test and killed by stereo decoded variant.|S5A:ACE graph tags/lyrics/duration/seed mismatch; S5A:PCM channels expected 2 got 1|
|R14|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_tags_can_supply_audio_prompt_when_prompt_is_absent @ L597; no argument params|Tags supply prompt when absent; status200 and graph tags match ambient synth pad.|Same ID; component: real audio route + backend transport fixture|P-ACE-BIND predicted killed by old and new graph assertions; M-PCM-MONO/sample corruption predicted survives old status/graph-only test and killed by stereo decoded variant.|S5A:ACE graph tags/lyrics/duration/seed mismatch; S5A:PCM channels expected 2 got 1|
|R15|tests/test_image_polling.py::TestImagePolling::test_fast_completion_returns_without_sleep @ L63; no argument params|At completion0 return exact image bytes, no error, elapsed0, no sleeps, one history check at0, no cancellation.|Same ID; component: real polling loop + MockTransport/virtual elapsed time|P-POLL predicted old/new kill: cadence, budget or cancellation regression. M-RB old byte equality already detects returned bytes changing, but invalid fixture offers no decoder validity evidence.|S5A:POLL original wait budget/cadence/cancellation violated; S5A:IMAGE decoded polling result differs|
|R16|tests/test_image_polling.py::TestImagePolling::test_slow_completion_keeps_polling @ L74; no argument params|Complete at0.8; exact image return; elapsed0.8..1.3; >1 checks; sleep cap0.5; no cancellation.|Same ID; component: real polling loop + MockTransport/virtual elapsed time|P-POLL predicted old/new kill: cadence, budget or cancellation regression. M-RB old byte equality already detects returned bytes changing, but invalid fixture offers no decoder validity evidence.|S5A:POLL original wait budget/cadence/cancellation violated; S5A:IMAGE decoded polling result differs|
|R17|tests/test_image_polling.py::TestImagePolling::test_slow_job_times_out_with_original_wait_budget @ L86; no argument params|Transient ConnectError, ReadTimeout,503 consume original2s budget; no images,504/image_generation_timeout,queue_position1; >4 checks; first0; initial sleeps0.05,0.1,0.2; max0.5; exactly one image-job cancellation.|Same ID; component: real polling loop + MockTransport/virtual elapsed time|P-POLL predicted old/new kill: cadence, budget or cancellation regression. M-RB old byte equality already detects returned bytes changing, but invalid fixture offers no decoder validity evidence.|S5A:POLL original wait budget/cadence/cancellation violated; S5A:IMAGE decoded polling result differs|
|R18|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_input_reference_switches_default_to_i2v_workflow @ L216; no argument params|Default model remains video-ltx-2.5-t2v publicly; resolved i2v, node70 uploaded-ref.png, strength0.7,200.|Same ID; component: real create handler + transport upload capture|P-I2V-BIND predicted old/new kill routing/strength mistakes. M-UPLOAD-RB predicted survives old fake upload (it ignores file content); new decoded capture kills.|S5A:I2V workflow/strength binding changed; S5A:UPLOAD input pixels differ|
|R19|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_explicit_image_strength_is_honored @ L227; no argument params|Explicit image_strength1.0 reaches node79 and status200.|Same ID; component: real create handler + transport upload capture|P-I2V-BIND predicted old/new kill routing/strength mistakes. M-UPLOAD-RB predicted survives old fake upload (it ignores file content); new decoded capture kills.|S5A:I2V workflow/strength binding changed; S5A:UPLOAD input pixels differ|
|R20|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_h3_input_reference_switches_to_i2v_without_strength_node @ L257; no argument params|minimax-h3 public name, H3 i2v resolved model, node20 uploaded-ref.png and node9 prompt x,200.|Same ID; component: real create handler + transport upload capture|P-I2V-BIND predicted old/new kill routing/strength mistakes. M-UPLOAD-RB predicted survives old fake upload (it ignores file content); new decoded capture kills.|S5A:I2V workflow/strength binding changed; S5A:UPLOAD input pixels differ|
|R21|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_retired_ltx23_name_with_input_reference_uses_ltx25_i2v @ L268; no argument params|Legacy ltx-2.3 public name resolves LTX2.5 i2v, node70 uploaded-ref.png,200.|Same ID; component: real create handler + transport upload capture|P-I2V-BIND predicted old/new kill routing/strength mistakes. M-UPLOAD-RB predicted survives old fake upload (it ignores file content); new decoded capture kills.|S5A:I2V workflow/strength binding changed; S5A:UPLOAD input pixels differ|
|R22|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_long_workflow_i2v_pins_first_frame @ L295; no argument params|Legacy ltx-2.3-long public name resolves LTX2.5-long, first frame uploaded-ref.png and strength1.0,200.|Same ID; component: real create handler + transport upload capture|P-I2V-BIND predicted old/new kill routing/strength mistakes. M-UPLOAD-RB predicted survives old fake upload (it ignores file content); new decoded capture kills.|S5A:I2V workflow/strength binding changed; S5A:UPLOAD input pixels differ|
|R23|tests/test_video_remix_replace.py::CharacterReplaceTests::test_json_create_binds_video_image_and_subjects @ L221; no argument params|Character replace200, public/canonical model, no remix lineage,832x448, source/ref upload binding, source/reference subjects,prompt,node22 length81.|Same ID; HTTP component: real create route + backend transport|P-REPLACE-BIND predicted old/new kill subject/graph/form/size-regression; M-UPLOAD-RB predicted survives old graph/length-only assertion and new decoded capture kills.|S5A:REPLACE graph/form/source binding changed; S5A:UPLOAD pixels differ; S5A:UPLOAD large reference truncated|
|R24|tests/test_video_remix_replace.py::CharacterReplaceTests::test_sdk_multipart_bracket_fields_are_rebuilt @ L237; no argument params|Bracketed source/reference input fields rebuild;200; seed5 and default source subject person reach graph.|Same ID; HTTP component: real create route + backend transport|P-REPLACE-BIND predicted old/new kill subject/graph/form/size-regression; M-UPLOAD-RB predicted survives old graph/length-only assertion and new decoded capture kills.|S5A:REPLACE graph/form/source binding changed; S5A:UPLOAD pixels differ; S5A:UPLOAD large reference truncated|
|R25|tests/test_video_remix_replace.py::CharacterReplaceTests::test_multipart_data_uri_larger_than_one_megabyte_is_accepted @ L252; no argument params|Multipart reference data URI >1MiB accepted,200; uploaded bytes >2MiB.|Same ID; HTTP component: real create route + backend transport|P-REPLACE-BIND predicted old/new kill subject/graph/form/size-regression; M-UPLOAD-RB predicted survives old graph/length-only assertion and new decoded capture kills.|S5A:REPLACE graph/form/source binding changed; S5A:UPLOAD pixels differ; S5A:UPLOAD large reference truncated|

## Concrete rewrite instructions by changed case

### R01: tests/test_comfy_workflow_metadata_catalog.py::test_gateway_png_reencodes_keep_comfy_chunks
Keep original black 8x8 offset, red-200 source and all-255 L mask subcases and chunk assertions. Add 8x8 asymmetric RGB(A) source, generated image, nonuniform mask and x/y offsets; decode full raster, compare independently calculated coordinate/composite oracle, preserve exact chunks.

### R02: tests/test_image_output.py::TestEncodeImageBytes::test_jpeg_encodes_and_drops_alpha
Keep current opaque (10,20,30,255) fixture and format/mode/MIME expectations. Add large asymmetric color patches, decode all pixels; compare stable interior RGB values to independent expected colors with narrow documented lossy tolerance and separate size assertion. R and B differ by far more than tolerance.

### R03: tests/test_image_output.py::TestEncodeImageBytes::test_webp_encodes_and_keeps_alpha
Keep opaque original subcase. Add asymmetric colored patches with alpha values 0,64,128,255; check exact alpha plane and decoded RGB on nontransparent patch interiors using justified lossy tolerance. Do not demand original RGB beneath fully transparent pixels.

### R04: tests/test_sync_media_routes.py::SyncMediaRouteTests::test_store_audio_response_stores_indexes_and_wraps_raw_pcm_as_wav
Keep legacy raw input, decode WAV and require channels=1, sample width=2, rate=24000, nframes=2, compression NONE and samples [1,2]. Replace capture-only storage with real store_asset_bytes using isolated cache; inspect stored asset and fresh DB read; preserve all job/owner assertions. Add signed/negative sample vector as separate subcase.

### R05: tests/test_sync_media_routes.py::SyncMediaRouteTests::test_build_image_response_payload_indexes_current_delivery_receipt
Keep existing 1x1 fixture subcase, real stores and response assertions. Add asymmetric raster; resolve response URL with owner through the internal resolver, decode delivered/cache PNG, compare exact raster and size plus real receipt/job/asset ids. Assert request creates one job; no fake payload producer.

### R06: tests/test_sync_media_routes.py::SyncMediaRouteTests::test_public_audio_speech_route_indexes_buffered_response_without_changing_delivered_bytes
Retain existing unchanged-byte scenario and captured provenance contract as legacy subcase. Add real provenance/store delivery with nonzero asymmetric stereo WAV and metadata mode full; decode audio before/after, verify channels/samples/frame count/rate and inspect stored metadata. Metadata attachment may alter bytes, so compare decoded samples while retaining original byte-preservation subcase.

### R07: tests/test_video_create_sdk_shape.py::VideoCreateMultipartParsingTests::test_input_reference_upload_becomes_data_uri
Retain old PNG-signature-plus-fakepayload normalization-only subcase unchanged (parser is not a decoder). Add valid asymmetric PNG subcase and verify byte identity, decoded size/mode/pixels, and file closed. Do not turn pure parser into an image-validation contract.

### R08: tests/test_video_create_sdk_shape.py::VideoCreateResponseEnvelopeTests::test_response_envelope_matches_sdk_video_model
Preserve original handwritten SDK fixture as explicit legacy subcase, then add real client.videos.create routed to ASGI/TestClient, capture actual response and compare SDK attributes to raw response and requested/canonical model. Pin required SDK version, require install/no broad skip; assert actual JSON field types and shape independently because model_construct is permissive. Current LTX 1280x720 request clamps to 1280x704 as existing route tests show; do not copy legacy fixture dimensions onto real route.

### R09: tests/test_video_extend_h3.py::ExtensionRouteTests::test_record_is_tracked_and_persisted_with_chain_info
Keep existing route, source record, 8-second extension and every chain assertion. Isolate media DB and receipt DB for this test only, initialize schemas, persist source through verified real API when available, reload record and receipt via fresh reads; compare in-memory and durable lineage/chain. Add separately named H3 cancellation composition cases below; do not rewrite global H3RouteBase or shared RemixReplaceBase.

### R10: tests/test_video_media_job_integration.py::VideoMediaJobIntegrationTests::test_executor_polls_video_route_and_stores_rendered_bytes
Keep exact lifecycle expectations, but use real create/status/content and asset store with an allow-listed test-local HTTP transport returning a deterministic decodable clip. Intercept backend calls/observable store bytes without replacing producers. Use event barrier/scripted poll sequence, fixed clock and isolated cache/DB. Assert private prompt absent from public metadata, exactly one job/asset/completed event, payload removed; decode stored video by independent decoder when runnable.

### R11: tests/test_video_workflow_execution.py::VideoWorkflowExecutionTests::test_manifest_video_workflow_runs_against_fake_comfyui
Keep passthrough byte-identity scenario (legacy opaque sentinel) separate from added real decodable input/output scenario. Preserve every graph binding and exact returned transport bytes. Replace no-op sleep only in this local test with scripted clock/event progression; decode frames in media variant, checking asymmetric colors, dimensions, count and source-vs-output distinction.

### R12: tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_tags_and_lyrics_bind_to_ace_step_inputs
Add a test-local override/injected response bytes for _call/FakeAudioClient without changing default helper behavior for untouched cases. Use valid non-silent stereo WAV, decode response and require exact channel/rate/frame/sample identity; retain all existing binding/status assertions and legacy field inputs. No ComfyUI/GPU call.

### R13: tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_prompt_falls_back_to_ace_step_tags
Add a test-local override/injected response bytes for _call/FakeAudioClient without changing default helper behavior for untouched cases. Use valid non-silent stereo WAV, decode response and require exact channel/rate/frame/sample identity; retain all existing binding/status assertions and legacy field inputs. No ComfyUI/GPU call.

### R14: tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_tags_can_supply_audio_prompt_when_prompt_is_absent
Add a test-local override/injected response bytes for _call/FakeAudioClient without changing default helper behavior for untouched cases. Use valid non-silent stereo WAV, decode response and require exact channel/rate/frame/sample identity; retain all existing binding/status assertions and legacy field inputs. No ComfyUI/GPU call.

### R15: tests/test_image_polling.py::TestImagePolling::test_fast_completion_returns_without_sleep
Use a helper-local valid asymmetric PNG transport fixture and retain exact byte equality before decoded raster assertions in success cases. Timeout preserves all errors/cancellation and no-image checks. Drive monotonic and sleep from same virtual clock if current producer reads monotonic (exact seam UNVERIFIED); never use real sleep or busy-loop no-op. Preserve completion thresholds as explicit cases.

### R16: tests/test_image_polling.py::TestImagePolling::test_slow_completion_keeps_polling
Use a helper-local valid asymmetric PNG transport fixture and retain exact byte equality before decoded raster assertions in success cases. Timeout preserves all errors/cancellation and no-image checks. Drive monotonic and sleep from same virtual clock if current producer reads monotonic (exact seam UNVERIFIED); never use real sleep or busy-loop no-op. Preserve completion thresholds as explicit cases.

### R17: tests/test_image_polling.py::TestImagePolling::test_slow_job_times_out_with_original_wait_budget
Use a helper-local valid asymmetric PNG transport fixture and retain exact byte equality before decoded raster assertions in success cases. Timeout preserves all errors/cancellation and no-image checks. Drive monotonic and sleep from same virtual clock if current producer reads monotonic (exact seam UNVERIFIED); never use real sleep or busy-loop no-op. Preserve completion thresholds as explicit cases.

### R18: tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_input_reference_switches_default_to_i2v_workflow
Do not mutate module PNG exported to unknown dependents. Each affected case supplies valid asymmetric PNG locally. Extend fake backend only to capture upload file bytes; assert decoded raster/size unchanged and retain all original graph/alias/strength/status assertions. Backend remains an external-boundary stub, never emulate real gateway routing.

### R19: tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_explicit_image_strength_is_honored
Do not mutate module PNG exported to unknown dependents. Each affected case supplies valid asymmetric PNG locally. Extend fake backend only to capture upload file bytes; assert decoded raster/size unchanged and retain all original graph/alias/strength/status assertions. Backend remains an external-boundary stub, never emulate real gateway routing.

### R20: tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_h3_input_reference_switches_to_i2v_without_strength_node
Do not mutate module PNG exported to unknown dependents. Each affected case supplies valid asymmetric PNG locally. Extend fake backend only to capture upload file bytes; assert decoded raster/size unchanged and retain all original graph/alias/strength/status assertions. Backend remains an external-boundary stub, never emulate real gateway routing.

### R21: tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_retired_ltx23_name_with_input_reference_uses_ltx25_i2v
Do not mutate module PNG exported to unknown dependents. Each affected case supplies valid asymmetric PNG locally. Extend fake backend only to capture upload file bytes; assert decoded raster/size unchanged and retain all original graph/alias/strength/status assertions. Backend remains an external-boundary stub, never emulate real gateway routing.

### R22: tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_long_workflow_i2v_pins_first_frame
Do not mutate module PNG exported to unknown dependents. Each affected case supplies valid asymmetric PNG locally. Extend fake backend only to capture upload file bytes; assert decoded raster/size unchanged and retain all original graph/alias/strength/status assertions. Backend remains an external-boundary stub, never emulate real gateway routing.

### R23: tests/test_video_remix_replace.py::CharacterReplaceTests::test_json_create_binds_video_image_and_subjects
Override reference_image in this case only; leave shared PNG, _body, RemixReplaceBase defaults unchanged for all dependents. Use valid asymmetric pixels; large-input case uses deterministic high-entropy or uncompressed PNG encoded to exceed existing byte thresholds, not junk appended to a header. Decode captured upload and compare full source raster. Keep every original request/graph/status/size assertion and no extra uploads.

### R24: tests/test_video_remix_replace.py::CharacterReplaceTests::test_sdk_multipart_bracket_fields_are_rebuilt
Override reference_image in this case only; leave shared PNG, _body, RemixReplaceBase defaults unchanged for all dependents. Use valid asymmetric pixels; large-input case uses deterministic high-entropy or uncompressed PNG encoded to exceed existing byte thresholds, not junk appended to a header. Decode captured upload and compare full source raster. Keep every original request/graph/status/size assertion and no extra uploads.

### R25: tests/test_video_remix_replace.py::CharacterReplaceTests::test_multipart_data_uri_larger_than_one_megabyte_is_accepted
Override reference_image in this case only; leave shared PNG, _body, RemixReplaceBase defaults unchanged for all dependents. Use valid asymmetric pixels; large-input case uses deterministic high-entropy or uncompressed PNG encoded to exceed existing byte thresholds, not junk appended to a header. Decode captured upload and compare full source raster. Keep every original request/graph/status/size assertion and no extra uploads.

## Additive cases, never substitutes for an old case
|Row|Proposed new case ID/parameters|Invariant and setup|Tier|Mutant/preservation distinction|Expected failure|
|---|---|---|---|---|---|
|A01|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_stereo_wav_delivery_preserves_channel_samples[channel_layout=stereo-distinct]|Valid stereo WAV at 24000Hz,16bit; frames [(1000,-2000),(3000,-4000),(-5000,6000),(7000,-8000)]. Real audio ingestion/delivery/cache; both channels and sample order survive. This does NOT require raw audio/pcm to imply stereo.|component/store|M-PCM-MONO + M-PCM-SAMPLES; no corresponding old stereo/sample oracle, strengthening|S5A:PCM channels expected 2 got 1; S5A:PCM sample frame1 channel1 expected -4000|
|A02|tests/test_video_extend_h3.py::H3CancellationReceiptTests::test_cancel_keeps_receipt_job_and_lineage[state=queued]|Create/extend actual H3 job with isolated real stores and source; request DELETE existing /v1/videos/{id}; backend confirms cancel. Preserve original extension receipt, durable chain/lineage and any route-generated cancellation receipt linked to same resource; cancellation precedes tombstone; no extra asset/job. Exact receipt schema/link hooks UNVERIFIED.|HTTP/store|M-RECEIPT-CANCEL; new receipt oracle only. Existing TestVideosDelete queued case is independent call-order survivor.|S5A:RECEIPT cancellation receipt missing; S5A:H3 cancel lost source lineage|
|A03|tests/test_video_extend_h3.py::H3CancellationReceiptTests::test_cancel_keeps_receipt_job_and_lineage[state=running]|Same as A02 after a controlled running/sampler event; real persisted state survives close/reopen. No sleep, use scripted events. Preserve original successful creation receipt status instead of rewriting history to canceled; new cancel-operation receipt records its own result. Exact status vocabulary UNVERIFIED.|HTTP/store|M-RECEIPT-CANCEL + M-H3-CANCEL-PERSIST; new composition oracle|S5A:RECEIPT cancellation receipt missing; S5A:H3 cancel state not durable|
|A04|tests/test_video_extend_h3.py::H3CancellationReceiptTests::test_cancel_failure_preserves_visibility_and_receipt[fault=connection-error]|Backend cancel raises ConnectionError;409/video_cancel_failed, no tombstone, source intact; original receipt remains; cancellation request failure receipt exists if route contract creates one. Route receipt creation contract UNVERIFIED, do not manufacture expected row in fixture.|HTTP/store|M-CANCEL-FALSE-SUCCESS; old TestVideosDelete failure test already protects409/no tombstone, receipt extension new|S5A:CANCEL failed backend was reported deleted; S5A:RECEIPT failed cancel audit missing|
|A05|tests/test_video_extend_h3.py::H3CancellationReceiptTests::test_cancel_terminal_race_preserves_receipts[result=cancelled-false]|Renderer returns200/cancelled:false after status race; preserve existing delete behavior200/tombstone, original receipt and successful cancel-operation outcome; do not assert that already-completed renderer job is canceled. Durable job final state should follow actual route contract (UNVERIFIED).|HTTP/store|M-CANCEL-RACE-409; old TestVideosDelete false-report case protects200/tombstone, receipt composition new|S5A:CANCEL terminal race rejected; S5A:RECEIPT terminal race lost audit|
|A06|tests/test_video_extend_h3.py::H3CancellationReceiptTests::test_non_owner_cancel_has_no_side_effects[owner=alice,caller=bob]|Other owner gets404; zero backend cancel/history calls and no owner record/asset mutation. Existing owner receipt unchanged; caller audit presence follows actual authorization policy, not assumed shared receipt access.|HTTP/security/store|M-CANCEL-OWNER-BYPASS; old TestVideosDelete owner case protects404/no tombstone, real store/no-call proof strengthening|S5A:CANCEL non-owner reached backend or mutated owner state|

## Per-file disposition and rewrite plan
|File|Static declarations|Plan|
|---|---:|---|
|[tests/test_collect_comfy_inventory.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_collect_comfy_inventory.py)|5|Retain 5 cases unchanged. Filesystem collector tests already use real temporary trees. Preserve redaction/no hashing unless supplied, safe class filtering, role mapping, validator handoff and CLI output. No database is relevant.|
|[tests/test_comfy_graph_signatures.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_comfy_graph_signatures.py)|8|Retain 8 declarations including all generated _specs/subTests. Preserve LTX 5 specs; H3 5 baseline plus contexts5,39,56; replacement base/fit/point; checker negative errors. Golden signature fixture and manifests are outside read scope; contents and expanded runtime results UNVERIFIED.|
|[tests/test_comfy_ui_graph.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_comfy_ui_graph.py)|12|Retain 12 declarations, widget/link/layout/default/cycle/immutability cases and real-twin oracle. Do not weaken exact frontend-authored twin checks to structural equality. Twin enumeration and fixture identities are UNVERIFIED because dependencies are not allow-listed.|
|[tests/test_comfy_workflow_metadata_catalog.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_comfy_workflow_metadata_catalog.py)|9|Rewrite R01 only; retain remaining8 declarations and PATHS parametrization/strict TangoFlux xfail. Add asymmetric offset/mask raster oracle to existing chunk-preservation test. Do not remove original uniform/mask-all-white controls. Keep full/none metadata-delivery test.|
|[tests/test_comfyui_workflow_validation.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_comfyui_workflow_validation.py)|8|Retain8 declarations. Preserve exact invalid-graph diagnostics, early loader rejection and imported-package resolution. Real temporary workflow files remain the appropriate fixture; no database substitution.|
|[tests/test_drain_openai_video_jobs.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_drain_openai_video_jobs.py)|6|Retain6 declarations. Existing now=time.time plus registry/get stubs cover six drain states. A separate timing/store modernization would need its own case mapping; do not silently replace these while implementing this slice. Current clock boundary behavior is a risk, not verified by this design.|
|[tests/test_gateway_drain.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_gateway_drain.py)|20|Retain20 declarations. Already uses injected FakeClock for state TTL/chat windows and a real admin SQLite DB. One SSE test has asyncio.sleep(0.01); do not claim deterministic coverage. Recommended future event barrier after first chunk, but no unledgered rewrite in this slice.|
|[tests/test_image_output.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_image_output.py)|36|Rewrite R02,R03; retain34 other declarations, including exact PNG passthrough and every validation/default/legacy-alias case. Add loss-tolerant decoded color checks and exact alpha checks, no compression-byte golden. Keep quality80 fixture plus explicitly named color/alpha cases.|
|[tests/test_image_polling.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_image_polling.py)|3|Rewrite R15-R17 with valid decoded PNG and virtual elapsed/sleep contract. Existing success thresholds0/0.8 and timeout2s are separate cases; preserve all transient-error sequence/cancel assertions. No real clock sleeps.|
|[tests/test_media_contract_validation.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_media_contract_validation.py)|14|Retain14 declarations and every explicit value loop unchanged (integer n types/bounds, edit dimensions/count/mask, audio encoding/type/duration/seed, model route bases/IDs and error envelope). Existing real _wav_bytes equality is already stronger than RIFF-only transport checking. Imported unscoped helpers remain unread.|
|[tests/test_media_fetch_security.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_media_fetch_security.py)|19|Retain19 declarations and all address/route/host/redirect parametrization. Preserve no-network and ownership/signature assertions, IP pinning/SNI/Host, DNS fail-closed, checked-address fallback, cap and redirects. Do not replace security mock transport with actual network.|
|[tests/test_media_ingestion.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_media_ingestion.py)|3|Retain3 declarations; already uses real SQLite. Preserve app-scoped owner key, idempotent first asset and invalid-list suppression. Template cloning is optional only after schema initialization/connection isolation are proven elsewhere; no need here.|
|[tests/test_sync_media_routes.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_sync_media_routes.py)|11|Rewrite R04-R06 only; retain8 declarations. Keep real media/receipt SQLite and isolated cache. Add mono sample-level wrap oracle, decoded image URL oracle, real-provenance stereo delivery variant. Preserve original byte-equality speech/provenance-spy subcase separately. Add A01 stereo regression; no shared _wav_bytes change since imported by other files.|
|[tests/test_tangoflux_audio_generate_manifest.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_tangoflux_audio_generate_manifest.py)|32|Rewrite R12-R14 only; retain29 declarations including all manifest/input/extension/rejection tests and non-string tags/lyrics loop. Inject valid stereo bytes only for rewritten route successes; default _call behavior unchanged for untouched consumers. Keep explicit field binding assertions as independent oracles.|
|[tests/test_uncensored_image_manifests.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_uncensored_image_manifests.py)|13|Retain13 declarations unchanged. Preserve Krea/SNOFS stock-equivalence apart from allowed LoRA/prefix, manifest model requirements, Anima bindings/discovery and stable aliases. No database fakes to replace.|
|[tests/test_video_create_sdk_shape.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_create_sdk_shape.py)|4|Rewrite R07,R08; retain2 other declarations for text-only multipart and disabled backend503. Preserve legacy opaque parser normalization and handwritten SDK fixture as labeled controls; add actual producer through pinned SDK. Required contract lane must fail missing/incorrect SDK, not skip. Exact version is UNVERIFIED until dependency lock is in scope.|
|[tests/test_video_extend.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_extend.py)|56|Retain56 declarations/subcases unchanged. LTX grid, lineage, caps, validation, ownership, uploaded/zero-byte-id SDK extension, aliases and progress/failure remain independently protected. Existing SDK-extend conditional skips are retained and must be reported, not counted as evidence.|
|[tests/test_video_extend_h3.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_extend_h3.py)|49|Rewrite R09 only; retain48 declarations and all model/context/shot loops. Do not change H3RouteBase/shared parent fixtures. Isolate/initialize real media and receipt stores within R09/additive new class; add A02-A06 cancellation/receipt composition cases. Existing code has no cancel test to preserve.|
|[tests/test_video_limits.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_limits.py)|9|Retain9 declarations including config/default/registry alias, capabilities and OpenAPI consistency. Preserve non-chain exclusions and canonically published workflow IDs. No parametrization rewrite needed.|
|[tests/test_video_list_delete_remix.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_list_delete_remix.py)|40|Retain40 declarations and all ownership/cancel/GC guards. They are valuable independent survivors for delete ordering,409 failure, cancel-false race, tombstone visibility, authenticated404 and indefinite completed record retention. Do not merge them into H3 cancellation additions.|
|[tests/test_video_ltx25_h3_workflows.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_ltx25_h3_workflows.py)|23|Rewrite R18-R22 only; retain18 other declarations. Capture valid image upload pixels while preserving real route/config/graph assertions and legacy aliases. Avoid changing exported PNG; inject valid image locally. Config/workflow actual data remains UNVERIFIED outside allowed tests.|
|[tests/test_video_media_job_http.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_media_job_http.py)|5|Retain5 declarations; these test pure adapters and MediaJob values, not a fake persistence engine. Preserve prompt-free metadata, precision4-decimal actual_seconds, MIME/URL/vid constraints and public shape. Do not force SQLite into pure adapters.|
|[tests/test_video_media_job_integration.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_media_job_integration.py)|8|Rewrite R10 only; retain7 existing real-SQLite admission/resume/content declarations. New executor path uses real route/asset producers with external backend stub, same running44-to-completed sequence and fixed clock. Real-store/cache wiring and exact hook implementations UNVERIFIED.|
|[tests/test_video_media_jobs.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_media_jobs.py)|8|Retain8 declarations/subcases. Preserve queued/running/succeeded/failed/canceled public mapping, metadata privacy, extraction/dedup/MIME and progress bounds. These are pure value/adaptor tests; no store fake to replace.|
|[tests/test_video_remix_replace.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_remix_replace.py)|31|Rewrite R23-R25 only; retain28 declarations. Valid asymmetric reference images and >2MiB valid PNG replace only local success fixtures. Shared RemixReplaceBase/PNG remain unchanged so test_video_extend/test_video_extend_h3/test_video_subject are unaffected. Do not weaken graph subjects, prompts, lengths or model behavior.|
|[tests/test_video_subject.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_subject.py)|18|Retain18 declarations and all point/warning/selection cases. Existing graph fitting/selection wiring is not raster-composition proof; retain it separately from image-edit compositing R01. Unknown imported workflow/config/helper contents UNVERIFIED.|
|[tests/test_video_workflow_execution.py](https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_workflow_execution.py)|2|Rewrite R11; retain downstream-output precedence test with both dictionary orders. Real executor plus external transport is the boundary; no fake gateway producer. Keep opaque passthrough case and add independent decoded input/output fixture.|

## Mutation prescriptions and exact-diff boundary
No production file was fetched. Consequently there are NO verified line-level production diffs in this report. Supplying patch context, line numbers or exact before/after expressions would fabricate source that was not in scope. The following are precise behavioral mutation prescriptions to instantiate only after production scope and execution are separately authorized. They must be small production-only changes, not alterations to test fixtures or expected values.

|Mutant|Production seam inferred only from scoped tests|Required mutation, while holding other behavior fixed|Red oracle and old/new meaning|Exact diff / run|
|---|---|---|---|---|
|M-RB|gateway.image_output.encode_image_bytes and the image offset/composite/delivery transformations called in proxy|Immediately before output encoding, permute each pixel (R,G,B,A) to (B,G,R,A). Preserve alpha, dimensions, container/MIME and metadata. Run encoder and compositor mutations separately.|R01/R02/R03/R05: asymmetric raster distinguishes RGB. Old R01 metadata-only and R02/R03 format/opaque-alpha checks should survive; new raster assertions fail. R05 metadata-only old also survives.|UNVERIFIED / NOT RUN|
|M-PCM-MONO|Audio storage/delivery path exercised by media_ingestion.store_audio_response and /v1/audio/speech or audio/generate|For explicitly stereo WAV input, retain a single channel but preserve total PCM byte length by padding/repetition; output a well-formed one-channel WAV. Do not merely truncate or create invalid RIFF. This is a right-byte-length/wrong-channel mutation.|A01, new stereo R06 and R12-R14 require channels=2 and exact distinct L/R samples. Old raw PCM R04 is correctly mono and cannot be called a stereo mutant killer. Old R06 fixture is mono silence. Byte length is insufficient.|UNVERIFIED / NOT RUN|
|M-SDK-SHAPE|Actual /v1/videos producer response, not handwritten payload in test_video_create_sdk_shape|Change serialized seconds from string to numeric, or object from video to video.generation, one field per run; preserve other keys and HTTP200.|R08 real producer + SDK + explicit raw JSON type/value assertion fails. Original handwritten Video.model_construct payload is disconnected and should survive production mutation. Record consumer behavior even if SDK accepts malformed shape; raw contract assertion must still fail.|UNVERIFIED / NOT RUN|
|M-RECEIPT-CANCEL|Real H3/video cancellation route's receipt persistence seam (exact function/path UNKNOWN)|Suppress only cancellation receipt persistence or remove the request-to-receipt linkage while leaving renderer cancel, response and durable video state unchanged. Do not suppress all request processing or invent a receipt in fixture.|A02/A03 require fresh-store-visible receipt composition and preserved original receipt/lineage. A04/A05 exercise failure/race composition once actual contract is verified. No H3 old case asserts this; this is additive strengthening, not preservation.|UNVERIFIED / NOT RUN|
|M-COMPOSE|proxy._offset_image_bytes / proxy._apply_image_edit_mask_composite|Reverse offset direction, ignore spatially varying edit mask, or choose wrong source layer, one change per run, while preserving metadata and dimensions.|R01 asymmetric/independent raster oracle. Existing uniform image/all-edit mask plus chunks does not establish spatial correctness. Mask polarity/offset semantics are UNVERIFIED until a production contract is inspected.|UNVERIFIED / NOT RUN|
|M-ALPHA|encode_image_bytes WEBP branch|Flatten nonopaque alpha to255 while returning a real WebP with correct RGB/container.|R03 nonopaque alpha plane fails; old fully opaque alpha255 fixture survives.|UNVERIFIED / NOT RUN|
|M-PCM-SAMPLES|PCM wrapper/audio delivery|Replace samples with zero or swap stereo channels while preserving WAV header, rate, sample width, channel count, frame count and payload byte length.|R04 exact mono samples [1,2], R06/R12-R14/A01 distinct stereo samples fail. Old RIFF-only R04 survives.|UNVERIFIED / NOT RUN|
|M-UPLOAD-RB|Reference-image normalization/upload preparation|Swap R/B by decoding and re-encoding uploaded PNG, keeping shape/MIME/filename/graph correct.|R18-R25 new captured-pixel check fails; old graph/name-only tests survive. R07 already checks exact byte identity and is preservation against upload-byte changes; do not mislabel it new-only.|UNVERIFIED / NOT RUN|
|M-VIDEO-RB|Video content/storage response boundary|Substitute a valid same-dimension/frame-count/fps clip with red/blue permuted frames while keeping MIME and lifecycle shape.|R10/R11 independent decoded frames fail. Legacy raw-byte passthrough controls can also detect content changes when the altered bytes pass their seam; old dummy bytes do not prove a decoder-level production transform.|UNVERIFIED / NOT RUN|
|M-H3-CANCEL-PERSIST|Durable cancellation/video-record update|Leave only transient in-memory cancellation/tombstone and omit durable update, with HTTP result unchanged.|A03 close/reopen reads fail; current generic in-memory deletion cases do not prove this composition.|UNVERIFIED / NOT RUN|
|M-CANCEL-FALSE-SUCCESS|Delete cancellation error branch|Continue tombstoning and return200/deleted after backend cancel raises instead of409.|Existing TestVideosDelete::test_cancel_failure_returns_409_not_deleted and A04 both must fail (preservation of generic behavior plus H3 composition).|UNVERIFIED / NOT RUN|
|M-CANCEL-RACE-409|Delete successful cancel-false branch|Treat HTTP200/cancelled:false as failure409 instead of terminal race success.|Existing TestVideosDelete::test_cancel_reports_false_still_tombstones and A05 both must fail.|UNVERIFIED / NOT RUN|
|M-CANCEL-OWNER-BYPASS|Ownership gate before delete/backend call|Permit caller Bob to cancel Alice's H3 video.|Existing TestVideosDelete::test_owner_cannot_be_deleted_by_a_different_credential and A06 must fail; new test additionally inspects backend call log and real durable state.|UNVERIFIED / NOT RUN|

### Preservation mutants for the P-* rows
Each prescription changes only the named protected output/operation; instantiate separate runs when a row lists several assertions. Their exact production diff and baseline/rewrite kill outcomes are all UNVERIFIED.
- P-META: omit prompt or workflow PNG text during offset/composite resave. Both old chunk equality and new checks must fail.
- P-JPEG: emit wrong MIME or wrong container/mode for JPEG. Both old/new format assertions must fail.
- P-WEBP: return wrong WebP MIME/container. Both old/new assertions must fail.
- P-PCM-WRAP: wrap PCM under wrong MIME/suffix, omit RIFF, or fail job/receipt asset linkage. Preserve negative sample mutation separately as M-PCM-SAMPLES.
- P-IMAGE-INDEX: index a different asset ID or omit app_image on receipt job. Both old/new current-delivery-index assertions must fail.
- P-SPEECH: leak extensions upstream, lose requested/resolved model provenance or mismatch receipt/job request IDs. Both old/new old-subcase assertions must fail; do not weaken unchanged-byte legacy scenario.
- P-UPLOAD: wrong data URI MIME, modified normalized bytes, or fail closing upload. Both old/new must fail; "close" test is independent of image validity.
- P-SDK-FIXTURE: this is a consumer fixture control, not production red/green evidence. The legacy fixture's id/object/status/progress/seconds/size expectations remain; only the added real-producer subcase can establish a production M-SDK-SHAPE kill.
- P-H3-CHAIN: omit persisted H3 record or change stored chain.segments from1, segment_frames from[187] or source_frames from244. Old test only checks some fields in persisted record (segments); new reload checks may strengthen field-level durability beyond old.
- P-EXECUTOR: skip completed event/payload deletion, wrong vid kind or .mp4 suffix, change index_library False or metadata NONE, or lose running44/succeeded100 execution state. Preserve every old explicit assertion; strengthen event cardinality/real-route composition separately.
- P-WORKFLOW: submit _meta, wrong source filename, multiplier or linked node4.b, return input echo instead of downstream output, or mismatch response MIME/status/bytes. Old/new explicit graph and passthrough assertions must fail.
- P-ACE-BIND: lose explicit tags/lyrics/duration12/seed1234, fail prompt fallback, or fail tags-as-prompt binding. All original case-specific graph assertions must survive verbatim.
- P-POLL: reset 2s timeout after transient failure, add initial sleep on immediate completion, exceed0.5s sleep cap, lose backoff0.05/0.1/0.2 or drop timeout cancel. Existing old/new time/sequence assertions must fail.
- P-I2V-BIND: select T2V despite input, lose legacy/public model echo or explicit/default strength, or bind wrong uploaded image node. Each case keeps its own LTX/H3/long route expectations.
- P-REPLACE-BIND: lose source/reference subject/default person/seed5, bracket reconstruction or accept-but-truncate large reference. Keep same no-lineage and graph/size assertions.

## Oracle and fixture specification
1. Pixels: use deterministic non-square or direction-distinguishing patterns, e.g. wide constant-color blocks with distinct R/B, one unique corner, a non-symmetric 8x8 per-pixel pattern for lossless offset/composition, and explicit alpha values0/64/128/255. RGB values must differ by more than any JPEG/WebP tolerance. For PNG, compare full decoded mode/size/raster exactly. For JPEG/WebP, compare non-edge patch interiors with a documented codec-specific tolerance; exact alpha where lossless alpha is promised. Do not derive expected pixels by calling the production compositor/encoder again. Offset wrap/crop and mask polarity must be resolved from an approved intended contract; currently UNVERIFIED.
2. WAV: parse with independent wave decoder, assert nchannels, sampwidth, framerate, nframes and compression NONE, then unpack interleaved signed little-endian samples. Keep original R04 [1,2] mono case. Add distinct stereo samples as shown in A01 and validate both stored and delivered payload. Do not infer stereo from audio/pcm;rate=24000 without an explicit supported channel contract.
3. Video: fixtures must be valid decodable clips with independently recorded frame count/fps/size and asymmetric color frames. Prefer a tiny lossless clip where supported; if route demands MP4, use pinned encoder settings and codec-aware pixel tolerances, not entire compressed-byte goldens. A fixture with only ftyp, PNG or RIFF magic is not decoded evidence. Existing fixtures/imported helpers are outside read scope, so their exact format/options are UNVERIFIED.
4. SQLite: initialize real media_jobs and receipt_store schemas under isolated temporary paths, or clone only a known initialized checkpointed template into a fresh path per case. Close connections/WAL before cloning. Reopen for durability assertions. Patch paths/clocks/external transport only; do not replace create/get/update/receipt completion with dictionary fakes. Existing real-store tests should remain real, rather than adding redundant storage to pure adapter tests.
5. Clocks: fixed wall time for created_at/expiry and matching monotonic/sleep virtual clock for polling. Use explicit events/barriers to stop at queued/running/cancel states. Backend responses are scripted transitions, not real sleeping or polling a no-op clock. No global asyncio.sleep patch that affects unrelated tasks.
6. SDK: pin one explicit verified openai-python release in the project's dependency mechanism, fail the required contract lane on absent/mismatched version, and exercise actual HTTP response producer through the SDK. Version selection/lock location is UNVERIFIED because dependencies are out of scope. model_construct is not validation; independent raw JSON field assertions are mandatory. Keep legacy fixture seconds='5'/size1280x720 control separate from actual route's model/grid-clamped response. Do not blindly model_validate the whole gateway extension payload against SDK enums without reviewing accepted gateway extensions.
7. Parametrization: preserve stable descriptive IDs for every newly added variant; old declarations remain stable in this proposal. Do not sweep old handwritten loops into fewer cases. Existing graph/catalog/data-driven parameters remain exactly as pinned; runtime expansion is not claimed. Optional future property testing should supplement finite examples, not replace them: image lossless identity/metadata separation; PCM sample roundtrip; monotonic polling budget; H3 frame-grid/context arithmetic. No mandatory property framework was named by this slice, and no new dependency is assumed.
8. Golden oracles: independent decoded pixel/sample arrays and known public response fields, not a snapshot generated by the same production function. Existing graph signature/workflow-twin exactness and metadata chunk strings remain valuable separate golden/structural checks. A golden update needs a stated contract reason.

## Execution proof protocol for an authorized implementation
This is a prescription, NOT a record of work performed:
- Freeze the baseline SHA, SDK/codec/dependency versions and collected case IDs including parameters; report skips separately.
- Run baseline untouched cases and rewritten cases on unmutated production; all old invariants must remain green.
- Apply one production mutant at a time after a verified exact diff is available. For P mutants run both old and rewritten affected cases and record each expected failure. For M strengthening mutants show old survivor where predicted and new failure, without counting collection/import errors or unrelated fixture failures as kills.
- Restore the exact original production bytes between mutations and rerun green; record mutation ID, exact diff, case ID, assertion diagnostic, command/environment and outcomes.
- Run full scoped suite, shared-fixture dependents and required repository checks only after authorization. No production edits or tests were made under this read-only assignment.
- Do not declare behavioral preservation passed merely because new cases fail mutants. Old assertion coverage/parameters and green base behavior must also be accounted for.

## Risks and limits
- The allow-list contains only tests; production, dependency locks, test conftest, media_test_bytes, _fake_comfy_video, workflow/config/manifests and fixture JSON are NOT read-authorized. Imports do not expand permission. No exact production patch or implementation assumption is verified by this report.
- H3 cancellation receipt behavior may expose a currently missing feature rather than a regression. The dedicated H3 cases are NEW. Verify whether DELETE creates a separate receipt and how it links to any media job. Do not fake/persist a row from the test merely to make the assertion pass; do not rewrite original creation-success history to cancellation. Record unsupported desired behavior as a gap until contract is established.
- A public /v1/videos job may be tracked differently from a /gateway/v1/media/jobs job. Do not fabricate a media_job_id linkage. Verify actual route/persistence composition before enforcing cross-store equalities.
- Old pixel/WebP alpha and WAV mono silence fixtures miss important content errors. Byte equality still matters for pure forwarding/normalization; replacing it wholesale with permissive decoded checks could weaken a separate byte-preservation contract.
- Lossy color tolerances that are too loose can let an R/B swap pass; use widely separated patch colors and independent expected values. Transparent RGB is not necessarily preserved under lossy encoding; alpha is the relevant exact oracle there.
- Same PCM byte length does not establish channels, frame count, duration or correct sample ordering. A default-mono test cannot kill a mono-downmix mutant.
- The actual SDK accepts model_construct without validation. Merely installing/pinning SDK or roundtripping a test-authored dictionary proves no producer behavior. Optional skips in unrelated retained SDK-extend tests must not be mistaken for coverage.
- Existing source-vs-rendered-output precedence and empty-upload video-id shortcut are intentional behaviors. Keep all guard cases; invalid image bytes are legitimate in a parser-only normalization control but not a decoded-media fixture.
- Shared fixtures are used across files, including unscoped callers. Keep changes local/optional with defaults unchanged. If implementing broader helper changes, expand the ledger FIRST for every affected case rather than treating fixture changes as free.
- Catalog/workflow counts and data-driven params depend on files outside scope. The 452 is source declarations, not collected test instances; total collected count and old runtime28s remain UNVERIFIED.
- Existing gateway drain SSE sleep and real time calls remain outside the chosen eleven-file rewrites. This slice does not claim to make the entire suite deterministic.
- No deletion or speed claim is justified. This is an oracle-quality proposal, and real decoding/store checks may add small cost.


## Appendix: complete static case-retention inventory
All 452 declarations are listed below. U means retained in the SAME file/class/test with all existing decorators, subTests, loops, assertions and helper defaults unchanged. Rxx maps to the complete preservation row above, also retaining the case ID. This inventory is source-derived, not pytest collection. Dynamic catalog/workflow parameter values depend on out-of-scope files and remain UNVERIFIED; no changed case's original arguments were omitted.

|Old case ID|Line|Disposition|
|---|---:|---|
|tests/test_collect_comfy_inventory.py::TestCollectComfyInventory::test_cli_outputs_redacted_json|155|U|
|tests/test_collect_comfy_inventory.py::TestCollectComfyInventory::test_collects_model_artifacts_by_role_without_hashing_or_absolute_paths|43|U|
|tests/test_collect_comfy_inventory.py::TestCollectComfyInventory::test_collects_safe_node_classes_from_object_info|24|U|
|tests/test_collect_comfy_inventory.py::TestCollectComfyInventory::test_includes_sha256_only_from_supplied_inventory|93|U|
|tests/test_collect_comfy_inventory.py::TestCollectComfyInventory::test_inventory_feeds_workflow_requirement_validator|125|U|
|tests/test_comfy_graph_signatures.py::ChainGraphSignatureTests::test_every_chain_graph_passes_the_signature_check|95|U|
|tests/test_comfy_graph_signatures.py::ChainGraphSignatureTests::test_manifest_reference_graph_passes|100|U|
|tests/test_comfy_graph_signatures.py::CheckerTests::test_it_catches_common_errors|161|U|
|tests/test_comfy_graph_signatures.py::H3ChainGraphSignatureTests::test_every_h3_chain_graph_passes_the_signature_check|126|U|
|tests/test_comfy_graph_signatures.py::H3ChainGraphSignatureTests::test_manifest_reference_graph_passes|131|U|
|tests/test_comfy_graph_signatures.py::ReplaceGraphSignatureTests::test_base_graph|143|U|
|tests/test_comfy_graph_signatures.py::ReplaceGraphSignatureTests::test_with_point_and_fit|151|U|
|tests/test_comfy_graph_signatures.py::ReplaceGraphSignatureTests::test_with_reference_fit|146|U|
|tests/test_comfy_ui_graph.py::test_control_after_generate_alignment|94|U|
|tests/test_comfy_ui_graph.py::test_cycle_returns_none|174|U|
|tests/test_comfy_ui_graph.py::test_dangling_link_and_empty_graph_return_none|168|U|
|tests/test_comfy_ui_graph.py::test_deterministic_and_json_serialisable|184|U|
|tests/test_comfy_ui_graph.py::test_input_not_mutated|195|U|
|tests/test_comfy_ui_graph.py::test_links_wiring_and_types|99|U|
|tests/test_comfy_ui_graph.py::test_optional_unlinked_connection_omitted_and_force_input_is_connection|202|U|
|tests/test_comfy_ui_graph.py::test_oracle_against_workflow_twins|241|U|
|tests/test_comfy_ui_graph.py::test_schema_envelope_and_layout|119|U|
|tests/test_comfy_ui_graph.py::test_unknown_class_returns_none|162|U|
|tests/test_comfy_ui_graph.py::test_widget_converted_to_input|144|U|
|tests/test_comfy_ui_graph.py::test_widget_value_defaults|136|U|
|tests/test_comfy_workflow_metadata_catalog.py::test_catalog_is_not_empty|44|U|
|tests/test_comfy_workflow_metadata_catalog.py::test_fixture_covers_every_catalog_node_type|51|U|
|tests/test_comfy_workflow_metadata_catalog.py::test_full_delivery_keeps_workflow_chunks_and_none_strips_them|160|U|
|tests/test_comfy_workflow_metadata_catalog.py::test_gateway_png_reencodes_keep_comfy_chunks|140|R01|
|tests/test_comfy_workflow_metadata_catalog.py::test_object_info_falls_back_to_last_good_disk_copy|101|U|
|tests/test_comfy_workflow_metadata_catalog.py::test_readable_node_ids_get_integer_ids|76|U|
|tests/test_comfy_workflow_metadata_catalog.py::test_submit_payload_embeds_workflow|66|U|
|tests/test_comfy_workflow_metadata_catalog.py::test_validator_reports_a_dropped_link|91|U|
|tests/test_comfy_workflow_metadata_catalog.py::test_workflow_derives_a_complete_ui_graph|59|U|
|tests/test_comfyui_workflow_validation.py::TestComfyUIWorkflowValidation::test_audio_and_video_loaders_reject_malformed_workflows|130|U|
|tests/test_comfyui_workflow_validation.py::TestComfyUIWorkflowValidation::test_node_must_be_mapping|47|U|
|tests/test_comfyui_workflow_validation.py::TestComfyUIWorkflowValidation::test_node_requires_class_type_and_inputs_mapping|53|U|
|tests/test_comfyui_workflow_validation.py::TestComfyUIWorkflowValidation::test_proxy_loader_rejects_malformed_workflow_before_ui_attachment|62|U|
|tests/test_comfyui_workflow_validation.py::TestComfyUIWorkflowValidation::test_proxy_loader_resolves_imported_package_workflow_artifacts|77|U|
|tests/test_comfyui_workflow_validation.py::TestComfyUIWorkflowValidation::test_valid_workflow_accepts_meta|23|U|
|tests/test_comfyui_workflow_validation.py::TestComfyUIWorkflowValidation::test_workflow_must_be_mapping|35|U|
|tests/test_comfyui_workflow_validation.py::TestComfyUIWorkflowValidation::test_workflow_must_have_non_meta_node|41|U|
|tests/test_drain_openai_video_jobs.py::UnfinishedOpenAIVideoJobsTests::test_counter_is_registered_with_the_drain|37|U|
|tests/test_drain_openai_video_jobs.py::UnfinishedOpenAIVideoJobsTests::test_counts_only_recent_unfinished_jobs|16|U|
|tests/test_drain_openai_video_jobs.py::UnfinishedOpenAIVideoJobsTests::test_genuinely_running_job_still_counts|77|U|
|tests/test_drain_openai_video_jobs.py::UnfinishedOpenAIVideoJobsTests::test_lost_completion_uses_cached_renderer_terminal_status|46|U|
|tests/test_drain_openai_video_jobs.py::UnfinishedOpenAIVideoJobsTests::test_stalled_job_past_expected_duration_does_not_block_drain|58|U|
|tests/test_drain_openai_video_jobs.py::UnfinishedOpenAIVideoJobsTests::test_terminal_gateway_media_job_does_not_block_drain|67|U|
|tests/test_gateway_drain.py::DrainAdminRouteTests::test_forwarded_for_header_cannot_fake_loopback|296|U|
|tests/test_gateway_drain.py::DrainAdminRouteTests::test_new_work_gets_503_while_draining|320|U|
|tests/test_gateway_drain.py::DrainAdminRouteTests::test_real_app_counts_in_flight_requests|341|U|
|tests/test_gateway_drain.py::DrainAdminRouteTests::test_refuses_non_loopback_peer_even_with_admin_key|287|U|
|tests/test_gateway_drain.py::DrainAdminRouteTests::test_rejects_bad_bodies|330|U|
|tests/test_gateway_drain.py::DrainAdminRouteTests::test_requires_admin_credential|280|U|
|tests/test_gateway_drain.py::DrainAdminRouteTests::test_status_then_drain_then_undrain|300|U|
|tests/test_gateway_drain.py::DrainStateTests::test_async_job_counters_count_as_in_flight|85|U|
|tests/test_gateway_drain.py::DrainStateTests::test_chat_quiet_window_tracking|49|U|
|tests/test_gateway_drain.py::DrainStateTests::test_counts_and_streams|33|U|
|tests/test_gateway_drain.py::DrainStateTests::test_flag_expires_by_ttl|94|U|
|tests/test_gateway_drain.py::DrainStateTests::test_path_rules|114|U|
|tests/test_gateway_drain.py::DrainStateTests::test_reads_do_not_count_as_chat_activity|80|U|
|tests/test_gateway_drain.py::DrainStateTests::test_ttl_is_clamped_and_flag_clears|106|U|
|tests/test_gateway_drain.py::InflightMiddlewareTests::test_counter_released_when_app_raises|177|U|
|tests/test_gateway_drain.py::InflightMiddlewareTests::test_draining_rejects_new_work_with_openai_shaped_503|203|U|
|tests/test_gateway_drain.py::InflightMiddlewareTests::test_draining_still_serves_reads_cancels_and_health|226|U|
|tests/test_gateway_drain.py::InflightMiddlewareTests::test_exempt_paths_are_not_counted|191|U|
|tests/test_gateway_drain.py::InflightMiddlewareTests::test_retry_after_env_override|248|U|
|tests/test_gateway_drain.py::InflightMiddlewareTests::test_sse_stays_counted_until_last_chunk|143|U|
|tests/test_image_output.py::TestBackgroundFormatConflict::test_opaque_jpeg_is_fine|150|U|
|tests/test_image_output.py::TestBackgroundFormatConflict::test_transparent_jpeg_conflicts|141|U|
|tests/test_image_output.py::TestBackgroundFormatConflict::test_transparent_png_is_fine|144|U|
|tests/test_image_output.py::TestBackgroundFormatConflict::test_transparent_webp_is_fine|147|U|
|tests/test_image_output.py::TestEncodeImageBytes::test_jpeg_encodes_and_drops_alpha|185|R02|
|tests/test_image_output.py::TestEncodeImageBytes::test_png_passes_through_unchanged|179|U|
|tests/test_image_output.py::TestEncodeImageBytes::test_unsupported_format_raises|207|U|
|tests/test_image_output.py::TestEncodeImageBytes::test_webp_encodes_and_keeps_alpha|195|R03|
|tests/test_image_output.py::TestParseOutputCompression::test_accepts_integer_in_range|69|U|
|tests/test_image_output.py::TestParseOutputCompression::test_bool_is_rejected|89|U|
|tests/test_image_output.py::TestParseOutputCompression::test_default_for_jpeg_is_100|66|U|
|tests/test_image_output.py::TestParseOutputCompression::test_default_for_png_is_none|63|U|
|tests/test_image_output.py::TestParseOutputCompression::test_fractional_float_above_is_rejected|93|U|
|tests/test_image_output.py::TestParseOutputCompression::test_fractional_float_below_is_rejected|100|U|
|tests/test_image_output.py::TestParseOutputCompression::test_fractional_string_is_rejected|109|U|
|tests/test_image_output.py::TestParseOutputCompression::test_non_integer_is_rejected|85|U|
|tests/test_image_output.py::TestParseOutputCompression::test_out_of_range_is_rejected|79|U|
|tests/test_image_output.py::TestParseOutputCompression::test_value_for_png_is_accepted_but_has_no_effect|72|U|
|tests/test_image_output.py::TestParseOutputCompression::test_whole_number_float_is_accepted|105|U|
|tests/test_image_output.py::TestParseOutputFormat::test_accepts_known_formats_case_insensitively|43|U|
|tests/test_image_output.py::TestParseOutputFormat::test_default_is_png|39|U|
|tests/test_image_output.py::TestParseOutputFormat::test_jpg_alias_normalizes_to_jpeg|48|U|
|tests/test_image_output.py::TestParseOutputFormat::test_non_string_is_rejected|56|U|
|tests/test_image_output.py::TestParseOutputFormat::test_unknown_format_is_rejected|51|U|
|tests/test_image_output.py::TestParseQuality::test_accepts_known_values|120|U|
|tests/test_image_output.py::TestParseQuality::test_dalle_legacy_hd_maps_to_high|132|U|
|tests/test_image_output.py::TestParseQuality::test_dalle_legacy_standard_maps_to_medium|129|U|
|tests/test_image_output.py::TestParseQuality::test_dalle_legacy_values_are_case_insensitive|135|U|
|tests/test_image_output.py::TestParseQuality::test_default_is_auto|117|U|
|tests/test_image_output.py::TestParseQuality::test_unknown_value_is_rejected|124|U|
|tests/test_image_output.py::TestQualityStepsFromManifest::test_auto_never_looks_up_manifest|155|U|
|tests/test_image_output.py::TestQualityStepsFromManifest::test_declared_mapping_resolves|165|U|
|tests/test_image_output.py::TestQualityStepsFromManifest::test_missing_manifest_key_returns_none|159|U|
|tests/test_image_output.py::TestQualityStepsFromManifest::test_non_integer_step_value_is_ignored|173|U|
|tests/test_image_output.py::TestQualityStepsFromManifest::test_none_manifest_returns_none|162|U|
|tests/test_image_output.py::TestQualityStepsFromManifest::test_value_not_declared_for_this_quality_returns_none|169|U|
|tests/test_image_polling.py::TestImagePolling::test_fast_completion_returns_without_sleep|63|R15|
|tests/test_image_polling.py::TestImagePolling::test_slow_completion_keeps_polling|74|R16|
|tests/test_image_polling.py::TestImagePolling::test_slow_job_times_out_with_original_wait_budget|86|R17|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_audio_native_success_and_seed_boundaries|171|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_edit_enforces_manifest_dimension_limits|100|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_edit_omitted_auto_and_valid_size_reach_generation|105|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_generation_accepts_integer_count_boundaries|70|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_http3_02_generation_n_is_a_bounded_integer|58|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_http3_03_edit_and_variations_reject_invalid_sizes|94|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_http3_04_excess_images_do_not_upload|118|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_http3_04_missing_required_mask_does_not_upload|127|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_http3_05_audio_rejects_requested_encoding|150|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_http3_06_audio_type_is_a_supported_string|155|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_http3_07_audio_duration_must_be_finite|160|U|
|tests/test_media_contract_validation.py::MediaContractValidationTests::test_http3_08_invalid_explicit_seed_is_not_randomized|165|U|
|tests/test_media_contract_validation.py::ModelRetrieveContractTests::test_http3_01_retrieve_for_both_openai_bases_and_prefixed_ids|194|U|
|tests/test_media_contract_validation.py::OpenAIErrorEnvelopeTests::test_every_error_carries_all_four_keys|216|U|
|tests/test_media_fetch_security.py::test_dns_rebinding_cannot_change_the_connected_host|90|U|
|tests/test_media_fetch_security.py::test_fetch_falls_back_only_to_previously_checked_addresses|120|U|
|tests/test_media_fetch_security.py::test_forwarded_host_minted_signed_asset_resolves_internally|265|U|
|tests/test_media_fetch_security.py::test_gateway_signed_image_url_resolves_without_network_with_ownership|226|U|
|tests/test_media_fetch_security.py::test_gateway_video_asset_signed_url_resolves_internally|250|U|
|tests/test_media_fetch_security.py::test_jpeg_base64_starting_with_slash_remains_accepted|212|U|
|tests/test_media_fetch_security.py::test_legacy_gateway_video_url_checks_ownership_and_streams_under_cap|313|U|
|tests/test_media_fetch_security.py::test_media_download_cap_applies_without_content_length|158|U|
|tests/test_media_fetch_security.py::test_media_download_rejects_declared_oversize_without_reading|167|U|
|tests/test_media_fetch_security.py::test_media_redirect_loop_is_bounded|183|U|
|tests/test_media_fetch_security.py::test_media_url_blocks_nonpublic_addresses_before_connecting|32|U|
|tests/test_media_fetch_security.py::test_mixed_public_private_dns_answers_fail_closed|139|U|
|tests/test_media_fetch_security.py::test_nat64_public_ipv4_destination_still_works|112|U|
|tests/test_media_fetch_security.py::test_operator_can_allow_exact_lan_host_but_not_metadata|146|U|
|tests/test_media_fetch_security.py::test_public_media_reference_uses_safe_fetch_boundary|64|U|
|tests/test_media_fetch_security.py::test_redirect_into_gateway_signed_asset_uses_internal_resolver|280|U|
|tests/test_media_fetch_security.py::test_redirect_to_private_address_is_rejected_before_second_hop|45|U|
|tests/test_media_fetch_security.py::test_saved_instance_base_url_identifies_internal_signed_assets|299|U|
|tests/test_media_fetch_security.py::test_video_create_rejects_existing_filesystem_path_with_400|196|U|
|tests/test_media_ingestion.py::SyncMediaIngestionTests::test_audio_speech_assets_use_audio_kind_and_idempotent_request|48|U|
|tests/test_media_ingestion.py::SyncMediaIngestionTests::test_invalid_or_empty_asset_lists_are_ignored|72|U|
|tests/test_media_ingestion.py::SyncMediaIngestionTests::test_sync_image_assets_are_indexed_as_succeeded_media_jobs|19|U|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_build_image_response_payload_indexes_current_delivery_receipt|205|R05|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_image_media_job_executor_uses_existing_job_without_sync_duplicate|269|U|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_index_receipt_assets_sets_job_id_and_does_not_duplicate_existing_job|122|U|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_index_receipt_assets_skips_when_executor_receipt_already_has_job_id|153|U|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_public_audio_speech_route_indexes_buffered_response_without_changing_delivered_bytes|337|R06|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_public_video_content_indexes_sync_delivery_asset|418|U|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_receipt_helpers_begin_finish_regular_and_streaming_responses|513|U|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_signed_audio_content_supports_range_and_metadata_mode_signature|234|U|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_store_audio_response_stores_indexes_and_wraps_raw_pcm_as_wav|171|R04|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_store_audio_response_uses_honest_aac_mime_and_skips_unknown_binary|394|U|
|tests/test_sync_media_routes.py::SyncMediaRouteTests::test_video_content_index_library_false_preserves_delivery_without_media_job|473|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateManifest::test_manifest_identity|449|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateManifest::test_namespaced_controls_validate_and_bind_to_ace_step|454|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateManifest::test_route_managed_lyrics_rejected_inside_extension_block|488|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_invalid_duration_is_rejected_before_submit|639|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_non_string_tags_and_lyrics_are_rejected|610|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_out_of_range_duration_is_rejected_before_submit|654|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_prompt_falls_back_to_ace_step_tags|583|R13|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_tags_and_lyrics_bind_to_ace_step_inputs|548|R12|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_tags_can_supply_audio_prompt_when_prompt_is_absent|597|R14|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_unknown_audio_field_detector_accepts_tags_and_lyrics|683|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_unknown_audio_model_returns_model_not_found|569|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAceStepAudioGenerateRoute::test_unknown_top_level_audio_control_is_rejected_before_submit|623|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAudioGenerateWorkflowExtensions::test_namespaced_controls_validate_and_bind_to_tangoflux|336|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAudioGenerateWorkflowExtensions::test_namespaced_tags_and_lyrics_are_route_managed|316|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAudioGenerateWorkflowExtensions::test_out_of_range_namespaced_control_is_rejected_not_applied|425|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAudioGenerateWorkflowExtensions::test_route_managed_namespaced_audio_fields_are_rejected|366|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAudioGenerateWorkflowExtensions::test_standard_audio_fields_allow_extensions_envelope|289|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAudioGenerateWorkflowExtensions::test_top_level_tags_and_lyrics_are_standard_route_fields|296|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAudioGenerateWorkflowExtensions::test_unknown_namespaced_control_is_rejected|406|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestAudioGenerateWorkflowExtensions::test_unknown_top_level_workflow_control_is_rejected|389|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestBindingShapeGuards::test_missing_target_node_fails_fast|275|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestBindsToCommittedWorkflow::test_all_inputs_apply_to_sampler_node|189|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestBindsToCommittedWorkflow::test_duration_contract_matches_comfy_node_minimum|226|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestBindsToCommittedWorkflow::test_out_of_range_inputs_are_rejected|230|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestBindsToCommittedWorkflow::test_prompt_is_required|221|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestBindsToCommittedWorkflow::test_unknown_input_is_rejected|255|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestBindsToCommittedWorkflow::test_wrong_type_is_rejected|246|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestShape::test_binding_targets_are_well_formed|172|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestShape::test_identity|153|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestShape::test_inputs_cover_workflow_controls|163|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestShape::test_intent_is_audio_generate|160|U|
|tests/test_tangoflux_audio_generate_manifest.py::TestManifestShape::test_output_is_audio_wav|169|U|
|tests/test_uncensored_image_manifests.py::TestAnimaTurboWorkflow::test_every_binding_target_exists|124|U|
|tests/test_uncensored_image_manifests.py::TestAnimaTurboWorkflow::test_identity_and_models|101|U|
|tests/test_uncensored_image_manifests.py::TestAnimaTurboWorkflow::test_inputs_bind_to_nodes|117|U|
|tests/test_uncensored_image_manifests.py::TestAnimaTurboWorkflow::test_workflow_loads_models_named_in_manifest|111|U|
|tests/test_uncensored_image_manifests.py::TestDiscoveryAndAliases::test_models_listing_includes_both_workflows_from_manifests|133|U|
|tests/test_uncensored_image_manifests.py::TestDiscoveryAndAliases::test_stable_aliases_resolve_and_default_image_is_unchanged|137|U|
|tests/test_uncensored_image_manifests.py::TestKrea2SnofsWorkflow::test_every_binding_target_exists|90|U|
|tests/test_uncensored_image_manifests.py::TestKrea2SnofsWorkflow::test_identity|41|U|
|tests/test_uncensored_image_manifests.py::TestKrea2SnofsWorkflow::test_inputs_bind_to_nodes|70|U|
|tests/test_uncensored_image_manifests.py::TestKrea2SnofsWorkflow::test_lora_sits_between_unet_and_sampler|47|U|
|tests/test_uncensored_image_manifests.py::TestKrea2SnofsWorkflow::test_lora_strength_range|86|U|
|tests/test_uncensored_image_manifests.py::TestKrea2SnofsWorkflow::test_manifest_requires_all_models|65|U|
|tests/test_uncensored_image_manifests.py::TestKrea2SnofsWorkflow::test_matches_stock_krea_apart_from_lora|55|U|
|tests/test_video_create_sdk_shape.py::VideoCreateMultipartParsingTests::test_handler_does_not_400_on_multipart_content_type|127|U|
|tests/test_video_create_sdk_shape.py::VideoCreateMultipartParsingTests::test_input_reference_upload_becomes_data_uri|98|R07|
|tests/test_video_create_sdk_shape.py::VideoCreateMultipartParsingTests::test_text_only_multipart_form_normalizes_to_json_body|81|U|
|tests/test_video_create_sdk_shape.py::VideoCreateResponseEnvelopeTests::test_response_envelope_matches_sdk_video_model|159|R08|
|tests/test_video_extend_h3.py::ConfigTests::test_h3_long_workflow_is_built_in_code_and_h3_models_lead_to_it|67|U|
|tests/test_video_extend_h3.py::ConfigTests::test_manifest_reference_graph_matches_the_builder|77|U|
|tests/test_video_extend_h3.py::ConfigTests::test_segment_length_cap_is_a_workflow_key|88|U|
|tests/test_video_extend_h3.py::ContextWindowTests::test_engine_reports_the_window_the_route_checks_the_source_against|59|U|
|tests/test_video_extend_h3.py::ContextWindowTests::test_window_is_a_whole_number_of_latent_steps|53|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_failure_names_the_segment_and_what_finished|525|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_i2v_first_frame_on_a_chain|471|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_invalid_shots_use_the_h3_minimum_shot_length|491|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_ltx_options_are_refused_on_h3_and_h3_ignores_negative_prompt|506|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_native_length_still_uses_the_template_graph|464|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_plain_seconds_past_the_native_length_is_the_same_chain|457|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_progress_units_are_registered_and_progress_spans_all_segments|514|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_shots_render_each_prompt_in_order_on_h3|437|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_size_follows_the_h3_limits|500|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_state_ledger_prefixes_every_segment|478|U|
|tests/test_video_extend_h3.py::CreateLongVideoTests::test_total_length_cap_applies|485|U|
|tests/test_video_extend_h3.py::ExtensionGraphTests::test_first_added_segment_pins_decoded_tail_and_audio_of_the_source|267|U|
|tests/test_video_extend_h3.py::ExtensionGraphTests::test_later_segments_of_an_extension_use_the_lossless_latent_path|284|U|
|tests/test_video_extend_h3.py::ExtensionGraphTests::test_silent_source_gives_a_silent_result|296|U|
|tests/test_video_extend_h3.py::ExtensionGraphTests::test_source_of_another_size_is_scaled_to_the_output_size|289|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_a_ltx_video_is_still_extended_by_ltx|349|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_a_model_without_a_chain_falls_back_to_the_default_extender|355|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_an_h3_video_is_extended_by_h3_without_naming_a_model|328|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_context_seconds_sets_the_window_in_whole_latent_steps|370|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_duration_adjustment_reason_names_the_h3_grid|428|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_long_extension_becomes_several_segments_with_one_prompt|375|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_model_can_be_named_and_aliases_work|361|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_options_depend_on_the_engine|406|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_record_is_tracked_and_persisted_with_chain_info|418|R09|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_source_shorter_than_the_context_window_is_rejected|390|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_source_that_is_not_24_fps_is_rejected_before_any_upload|398|U|
|tests/test_video_extend_h3.py::ExtensionRouteTests::test_total_length_cap_returns_the_same_clear_400|382|U|
|tests/test_video_extend_h3.py::GraphTests::test_added_segment_pins_the_previous_latent_not_decoded_frames|168|U|
|tests/test_video_extend_h3.py::GraphTests::test_context_length_follows_the_context_window|209|U|
|tests/test_video_extend_h3.py::GraphTests::test_each_segment_has_its_own_prompt_and_seed|191|U|
|tests/test_video_extend_h3.py::GraphTests::test_every_sampled_length_is_on_the_17k_plus_5_grid|157|U|
|tests/test_video_extend_h3.py::GraphTests::test_first_frame_image_pins_the_first_segment_only|216|U|
|tests/test_video_extend_h3.py::GraphTests::test_first_segment_has_no_context_and_trims_nothing|162|U|
|tests/test_video_extend_h3.py::GraphTests::test_refs_resolve_and_only_h3_models_are_loaded|148|U|
|tests/test_video_extend_h3.py::GraphTests::test_sampled_length_is_context_plus_new_frames|185|U|
|tests/test_video_extend_h3.py::GraphTests::test_segment_of_node_maps_progress_nodes|223|U|
|tests/test_video_extend_h3.py::GraphTests::test_total_frames_and_audio_match|196|U|
|tests/test_video_extend_h3.py::PlanTests::test_a_long_shot_splits_into_near_equal_segments_with_one_prompt|108|U|
|tests/test_video_extend_h3.py::PlanTests::test_added_segments_grow_in_steps_of_17_within_the_cap|100|U|
|tests/test_video_extend_h3.py::PlanTests::test_extension_has_no_first_segment|115|U|
|tests/test_video_extend_h3.py::PlanTests::test_first_segment_is_17a_plus_5|93|U|
|tests/test_video_extend_h3.py::PlanTests::test_source_of_another_frame_rate_is_refused|127|U|
|tests/test_video_extend_h3.py::PlanTests::test_state_ledger_is_prepended_and_empty_shots_are_an_error|120|U|
|tests/test_video_extend_h3.py::ProgressOrderTests::test_a_later_segments_encode_does_not_move_progress|235|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_i2v_first_frame_on_a_chain|524|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_invalid_shots|492|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_long_names_keep_working|477|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_model_without_a_chain_rejects_shots_and_long_seconds|510|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_native_length_still_uses_the_template_graph|471|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_plain_seconds_past_native_length_is_the_same_chain|447|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_progress_units_are_registered_for_the_job|531|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_sdk_multipart_shots_are_regrouped|454|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_shots_as_a_json_string|464|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_shots_render_each_prompt_in_order|426|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_shots_work_on_the_default_ltx_model_too|441|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_state_ledger_prefixes_every_segment|517|U|
|tests/test_video_extend.py::CreateLongVideoTests::test_total_length_cap_applies_to_create|485|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_501_when_not_configured|304|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_an_empty_upload_not_named_by_a_video_id_is_still_rejected|341|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_context_seconds_sets_the_overlap_window|225|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_default_cap_is_40_seconds|251|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_extension_returns_a_video_resource_with_lineage_and_stitched_seconds|191|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_graph_loads_the_uploaded_source_and_adds_one_segment|210|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_long_extensions_become_several_segments_with_one_prompt|219|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_model_that_cannot_extend_is_rejected|299|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_openai_prefixed_alias_route_exists|390|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_other_keys_video_cannot_be_extended|266|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_record_is_tracked_and_persisted_with_chain_info|254|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_sdk_multipart_shape_with_bracketed_video_id|381|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_sdk_zero_byte_upload_named_by_video_id_extends_that_video|330|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_source_shorter_than_the_context_window_is_rejected|310|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_state_ledger_goes_in_front_of_the_prompt|229|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_the_empty_upload_shortcut_is_not_available_on_other_video_fields|347|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_the_real_sdk_extends_an_uploaded_video|372|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_the_real_sdk_extends_by_id_and_parses_the_result|358|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_total_length_cap_is_a_config_key|243|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_total_length_cap_returns_a_clear_400|233|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_unknown_source_id_is_404_when_auth_is_on|273|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_unreadable_source_is_rejected_before_any_upload|317|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_uploaded_source_works_and_has_no_lineage|323|U|
|tests/test_video_extend.py::ExtensionRouteTests::test_validation_errors|278|U|
|tests/test_video_extend.py::FailureInfoTests::test_error_outside_a_segment_keeps_the_plain_message|410|U|
|tests/test_video_extend.py::FailureInfoTests::test_failed_chain_names_the_segment_and_what_finished|397|U|
|tests/test_video_extend.py::GraphTests::test_added_segment_conditions_on_the_last_frames_not_one_frame|99|U|
|tests/test_video_extend.py::GraphTests::test_audio_length_equals_video_length|117|U|
|tests/test_video_extend.py::GraphTests::test_each_segment_has_its_own_prompt_and_seed|111|U|
|tests/test_video_extend.py::GraphTests::test_first_frame_image_pins_the_first_segment_only|145|U|
|tests/test_video_extend.py::GraphTests::test_graph_refs_resolve_and_only_ltx25_models_are_loaded|89|U|
|tests/test_video_extend.py::GraphTests::test_segment_of_node_maps_progress_nodes|152|U|
|tests/test_video_extend.py::GraphTests::test_silent_source_gives_a_silent_result|139|U|
|tests/test_video_extend.py::GraphTests::test_source_of_another_size_is_scaled_to_the_output_size|134|U|
|tests/test_video_extend.py::GraphTests::test_source_video_is_loaded_continued_and_counted|125|U|
|tests/test_video_extend.py::PlanTests::test_empty_shots_are_an_error_with_a_code|75|U|
|tests/test_video_extend.py::PlanTests::test_every_added_segment_is_a_multiple_of_8_and_within_the_cap|53|U|
|tests/test_video_extend.py::PlanTests::test_extension_has_no_first_segment|67|U|
|tests/test_video_extend.py::PlanTests::test_first_segment_is_8a_plus_1|42|U|
|tests/test_video_extend.py::PlanTests::test_plain_seconds_split_into_a_first_and_added_segments|47|U|
|tests/test_video_extend.py::PlanTests::test_shots_keep_their_own_prompt_and_index|62|U|
|tests/test_video_extend.py::PlanTests::test_state_ledger_is_prepended_to_every_segment|71|U|
|tests/test_video_extend.py::ProgressTests::test_progress_spans_all_segments_and_never_runs_backwards|160|U|
|tests/test_video_limits.py::VideoLimitsDiscoveryTests::test_capabilities_carry_the_cap_and_segment_limits|77|U|
|tests/test_video_limits.py::VideoLimitsDiscoveryTests::test_video_alias_carries_the_same_published_limits_as_its_target|100|U|
|tests/test_video_limits.py::VideoLimitsDiscoveryTests::test_workflow_catalog_carries_the_same_limits|112|U|
|tests/test_video_limits.py::VideoLimitsTests::test_a_chain_workflow_does_not_publish_its_template_second_range_as_native_length|38|U|
|tests/test_video_limits.py::VideoLimitsTests::test_cap_defaults_to_the_enforced_default_and_follows_config|48|U|
|tests/test_video_limits.py::VideoLimitsTests::test_chain_capable_workflows_publish_the_total_cap_and_their_segment_length|29|U|
|tests/test_video_limits.py::VideoLimitsTests::test_lookup_accepts_config_keys_and_registry_ids|53|U|
|tests/test_video_limits.py::VideoLimitsTests::test_plain_and_dangling_workflows_have_no_cap|42|U|
|tests/test_video_limits.py::VideoLimitsTests::test_the_published_cap_is_the_enforced_cap|60|U|
|tests/test_video_list_delete_remix.py::TestVideoJobRecordGC::test_completed_job_outlives_the_old_five_minute_terminal_window|688|U|
|tests/test_video_list_delete_remix.py::TestVideoJobRecordGC::test_completed_job_survives_indefinitely|696|U|
|tests/test_video_list_delete_remix.py::TestVideoJobRecordGC::test_terminal_progress_gc_no_longer_touches_job_records|709|U|
|tests/test_video_list_delete_remix.py::TestVideoJobRecordGC::test_tombstone_is_retained_briefly_then_reaped|702|U|
|tests/test_video_list_delete_remix.py::TestVideosDelete::test_cancel_failure_returns_409_not_deleted|494|U|
|tests/test_video_list_delete_remix.py::TestVideosDelete::test_cancel_reports_false_still_tombstones|518|U|
|tests/test_video_list_delete_remix.py::TestVideosDelete::test_deleted_job_no_longer_visible_to_status_or_content|540|U|
|tests/test_video_list_delete_remix.py::TestVideosDelete::test_invalid_id_is_400|432|U|
|tests/test_video_list_delete_remix.py::TestVideosDelete::test_known_but_not_yet_in_history_still_deletable|466|U|
|tests/test_video_list_delete_remix.py::TestVideosDelete::test_known_terminal_job_deletes_without_cancelling|438|U|
|tests/test_video_list_delete_remix.py::TestVideosDelete::test_owner_can_delete_own_job|569|U|
|tests/test_video_list_delete_remix.py::TestVideosDelete::test_owner_cannot_be_deleted_by_a_different_credential|559|U|
|tests/test_video_list_delete_remix.py::TestVideosDelete::test_queued_job_is_cancelled_before_tombstoning|478|U|
|tests/test_video_list_delete_remix.py::TestVideosDelete::test_unknown_id_is_404|426|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_after_cursor_excludes_up_to_and_including|291|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_bad_after_cursor_rejected|323|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_bad_limit_rejected|309|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_bad_order_rejected|316|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_default_order_is_newest_first|244|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_deleted_job_excluded_from_list|394|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_empty_list|231|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_entry_shape_includes_required_fields|330|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_evicted_after_cursor_errors_instead_of_restarting|373|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_failed_entry_includes_error|346|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_limit_and_has_more|274|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_list_scoped_to_owner|405|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_order_asc|259|U|
|tests/test_video_list_delete_remix.py::TestVideosList::test_running_entry_not_yet_in_history_uses_queue|363|U|
|tests/test_video_list_delete_remix.py::TestVideosOwnership::test_content_hidden_from_non_owner|601|U|
|tests/test_video_list_delete_remix.py::TestVideosOwnership::test_status_hidden_from_non_owner|585|U|
|tests/test_video_list_delete_remix.py::TestVideosOwnership::test_status_visible_to_owner|592|U|
|tests/test_video_list_delete_remix.py::TestVideosOwnership::test_untracked_id_is_404_for_an_authenticated_key|608|U|
|tests/test_video_list_delete_remix.py::TestVideosRemix::test_non_owner_gets_404_not_remix_error|665|U|
|tests/test_video_list_delete_remix.py::TestVideosRemix::test_shipped_config_enables_remix_with_a_matching_alias|674|U|
|tests/test_video_list_delete_remix.py::TestVideosRemix::test_unconfigured_returns_stable_501|652|U|
|tests/test_video_list_delete_remix.py::TestVideosRemix::test_unknown_id_is_404|639|U|
|tests/test_video_list_delete_remix.py::TestVideosStatusAndContent::test_content_for_known_unfinished_job_returns_not_ready|214|U|
|tests/test_video_list_delete_remix.py::TestVideosStatusAndContent::test_status_maps_history_success_to_openai_completed|176|U|
|tests/test_video_list_delete_remix.py::TestVideosStatusAndContent::test_status_maps_internal_running_to_openai_in_progress|139|U|
|tests/test_video_list_delete_remix.py::TestVideosStatusAndContent::test_status_progress_is_monotonic_when_history_lags_live_progress|194|U|
|tests/test_video_ltx25_h3_workflows.py::ModelDiscoveryTests::test_every_listed_media_id_resolves_on_its_route|68|U|
|tests/test_video_ltx25_h3_workflows.py::ModelDiscoveryTests::test_new_media_models_are_listed_with_honest_descriptions|47|U|
|tests/test_video_ltx25_h3_workflows.py::SizeAndLengthRuleTests::test_default_size_rule_is_unchanged|25|U|
|tests/test_video_ltx25_h3_workflows.py::SizeAndLengthRuleTests::test_length_rule_h3_is_17k_plus_5|39|U|
|tests/test_video_ltx25_h3_workflows.py::SizeAndLengthRuleTests::test_length_rule_ltx_is_8k_plus_1|36|U|
|tests/test_video_ltx25_h3_workflows.py::SizeAndLengthRuleTests::test_size_multiple_64_floors_and_clamps_to_max|29|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_default_model_t2v_sets_64_multiple_size_and_frames|189|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_explicit_image_strength_is_honored|227|R19|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_h3_input_reference_switches_to_i2v_without_strength_node|257|R20|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_h3_t2v_uses_prompt_field_and_17k_plus_5_frames|242|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_i2v_model_without_input_reference_is_rejected|235|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_input_reference_switches_default_to_i2v_workflow|216|R18|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_long_workflow_builds_a_segment_chain|278|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_long_workflow_i2v_pins_first_frame|295|R22|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_long_workflow_is_built_in_code|174|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_ltx25_is_the_default_video_model|137|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_manifest_reference_graph_matches_the_builder|178|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_no_ltx23_workflow_remains_in_config|170|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_odd_size_floors_to_64_multiple|202|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_omitted_size_uses_workflow_default|211|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_retired_ltx23_name_with_input_reference_uses_ltx25_i2v|268|R21|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_retired_ltx23_names_resolve_to_ltx25|146|U|
|tests/test_video_ltx25_h3_workflows.py::VideoCreateGraphTests::test_unknown_video_model_returns_model_not_found|127|U|
|tests/test_video_media_job_http.py::VideoMediaJobHttpAdapterTests::test_completed_asset_from_stored_video_requires_vid_handle_and_url|127|U|
|tests/test_video_media_job_http.py::VideoMediaJobHttpAdapterTests::test_content_response_requires_succeeded_job_with_asset|157|U|
|tests/test_video_media_job_http.py::VideoMediaJobHttpAdapterTests::test_execution_result_builds_update_kwargs_and_event|48|U|
|tests/test_video_media_job_http.py::VideoMediaJobHttpAdapterTests::test_generation_and_content_responses_from_completed_job|79|U|
|tests/test_video_media_job_http.py::VideoMediaJobHttpAdapterTests::test_request_body_builds_prompt_free_durable_job|14|U|
|tests/test_video_media_job_integration.py::VideoMediaJobIntegrationTests::test_content_route_returns_signed_video_asset_payload|282|U|
|tests/test_video_media_job_integration.py::VideoMediaJobIntegrationTests::test_direct_video_submit_uses_media_job_admission_overrides|117|U|
|tests/test_video_media_job_integration.py::VideoMediaJobIntegrationTests::test_direct_video_submit_uses_resource_admission|96|U|
|tests/test_video_media_job_integration.py::VideoMediaJobIntegrationTests::test_direct_video_submit_uses_service_availability_overrides|150|U|
|tests/test_video_media_job_integration.py::VideoMediaJobIntegrationTests::test_executor_polls_video_route_and_stores_rendered_bytes|183|R10|
|tests/test_video_media_job_integration.py::VideoMediaJobIntegrationTests::test_generic_submit_creates_video_job_without_prompt_metadata|60|U|
|tests/test_video_media_job_integration.py::VideoMediaJobIntegrationTests::test_resume_pending_video_jobs_marks_missing_payload_failed|340|U|
|tests/test_video_media_job_integration.py::VideoMediaJobIntegrationTests::test_resume_pending_video_jobs_reschedules_jobs_with_payloads|313|U|
|tests/test_video_media_jobs.py::VideoAssetExtractionTests::test_extracts_and_normalizes_comfyui_video_outputs|65|U|
|tests/test_video_media_jobs.py::VideoAssetExtractionTests::test_normalized_asset_rejects_empty_filename|107|U|
|tests/test_video_media_jobs.py::VideoMediaJobMetadataTests::test_builds_prompt_free_video_generate_job|14|U|
|tests/test_video_media_jobs.py::VideoMediaJobMetadataTests::test_metadata_uses_media_job_safety_guard|45|U|
|tests/test_video_media_jobs.py::VideoStatusShapeTests::test_progress_validation_rejects_out_of_range_values|192|U|
|tests/test_video_media_jobs.py::VideoStatusShapeTests::test_status_event_uses_client_visible_progress_keys|113|U|
|tests/test_video_media_jobs.py::VideoStatusShapeTests::test_video_generation_payload_maps_openai_poll_statuses|171|U|
|tests/test_video_media_jobs.py::VideoStatusShapeTests::test_video_generation_payload_matches_route_shape|135|U|
|tests/test_video_remix_replace.py::CharacterReplaceTests::test_input_reference_is_not_a_second_way_to_pass_the_image|302|U|
|tests/test_video_remix_replace.py::CharacterReplaceTests::test_json_create_binds_video_image_and_subjects|221|R23|
|tests/test_video_remix_replace.py::CharacterReplaceTests::test_missing_inputs_are_rejected_before_any_upload|266|U|
|tests/test_video_remix_replace.py::CharacterReplaceTests::test_multipart_data_uri_larger_than_one_megabyte_is_accepted|252|R25|
|tests/test_video_remix_replace.py::CharacterReplaceTests::test_sdk_multipart_bracket_fields_are_rebuilt|237|R24|
|tests/test_video_remix_replace.py::CharacterReplaceTests::test_short_source_clamps_default_seconds_to_its_length|308|U|
|tests/test_video_remix_replace.py::CharacterReplaceTests::test_source_video_must_be_a_reference_not_a_path|297|U|
|tests/test_video_remix_replace.py::CharacterReplaceTests::test_stable_alias_and_names_resolve|283|U|
|tests/test_video_remix_replace.py::CharacterReplaceTests::test_unknown_extension_and_route_managed_input_are_rejected|291|U|
|tests/test_video_remix_replace.py::EditRouteTests::test_existing_video_by_id_sets_remixed_from|176|U|
|tests/test_video_remix_replace.py::EditRouteTests::test_form_style_video_id_reference|182|U|
|tests/test_video_remix_replace.py::EditRouteTests::test_missing_video_and_prompt_are_rejected|187|U|
|tests/test_video_remix_replace.py::EditRouteTests::test_upload_remix_has_no_source_video_id|162|U|
|tests/test_video_remix_replace.py::EditRouteTests::test_url_source_is_downloaded|194|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_client_model_name_is_echoed|98|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_other_keys_video_is_hidden|132|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_remix_creates_a_new_job_that_points_at_the_source|67|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_remix_is_501_when_not_configured|146|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_remix_record_is_tracked_for_status_and_list|89|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_remix_requires_prompt|116|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_seconds_beyond_the_source_is_rejected|110|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_seed_and_seconds_are_accepted|104|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_source_not_completed_is_rejected_with_video_not_ready|126|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_source_that_is_not_a_readable_video_is_rejected|152|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_source_video_cannot_be_overridden_by_an_extension|138|U|
|tests/test_video_remix_replace.py::RemixRouteTests::test_unknown_source_is_404|121|U|
|tests/test_video_remix_replace.py::VideoInputsUnitTests::test_bracket_form_rebuilds_nested_mapping|319|U|
|tests/test_video_remix_replace.py::VideoInputsUnitTests::test_bracket_form_rejects_scalar_container_conflicts|327|U|
|tests/test_video_remix_replace.py::VideoInputsUnitTests::test_coerce_form_scalars_follows_manifest_types|340|U|
|tests/test_video_remix_replace.py::VideoInputsUnitTests::test_snap_length_down|335|U|
|tests/test_video_remix_replace.py::VideoInputsUnitTests::test_suffix_prefers_filename_then_mime|345|U|
|tests/test_video_subject.py::ReplaceSubjectRouteTests::test_default_replaces_the_largest_person|133|U|
|tests/test_video_subject.py::ReplaceSubjectRouteTests::test_finished_job_carries_the_warning|169|U|
|tests/test_video_subject.py::ReplaceSubjectRouteTests::test_form_strings_are_converted|150|U|
|tests/test_video_subject.py::ReplaceSubjectRouteTests::test_invalid_selection_is_a_400_with_the_param|160|U|
|tests/test_video_subject.py::ReplaceSubjectRouteTests::test_no_warning_when_the_person_was_chosen|180|U|
|tests/test_video_subject.py::ReplaceSubjectRouteTests::test_point_uses_the_output_size|143|U|
|tests/test_video_subject.py::ReplaceSubjectRouteTests::test_reference_fit_is_on_by_default_and_can_be_turned_off|121|U|
|tests/test_video_subject.py::ReplaceSubjectRouteTests::test_selection_reaches_the_fit_nodes|129|U|
|tests/test_video_subject.py::ReplaceSubjectRouteTests::test_subject_index_is_bound|138|U|
|tests/test_video_subject.py::SubjectHelperTests::test_bad_points|55|U|
|tests/test_video_subject.py::SubjectHelperTests::test_explicit_selection|105|U|
|tests/test_video_subject.py::SubjectHelperTests::test_no_point_leaves_the_graph_alone|49|U|
|tests/test_video_subject.py::SubjectHelperTests::test_point_rewires_tracking_to_one_person|38|U|
|tests/test_video_subject.py::SubjectHelperTests::test_reference_fit_replaces_the_cropped_reference_everywhere|61|U|
|tests/test_video_subject.py::SubjectHelperTests::test_reference_fit_uses_the_same_person_selection|75|U|
|tests/test_video_subject.py::SubjectHelperTests::test_tracked_count_reads_the_scores|87|U|
|tests/test_video_subject.py::SubjectHelperTests::test_warning_rules|94|U|
|tests/test_video_subject.py::SubjectHelperTests::test_workflow_selects_the_largest_person_and_masks_the_reference|28|U|
|tests/test_video_workflow_execution.py::VideoWorkflowExecutionTests::test_manifest_video_workflow_runs_against_fake_comfyui|64|R11|
|tests/test_video_workflow_execution.py::VideoWorkflowExecutionTests::test_video_output_prefers_downstream_result_over_input_echo|49|U|

## GitHub read receipts
All test contents below were returned at the requested commit; blob SHAs identify individual file content. No file outside this list was read.
|Path|Blob SHA|Source|
|---|---|---|
|tests/test_collect_comfy_inventory.py|0b7c1493195d611f1f44f84ed4d0402631e0478d|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_collect_comfy_inventory.py|
|tests/test_comfy_graph_signatures.py|17b9ce6ff91d43e1b33bea5db3c155302d72d83e|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_comfy_graph_signatures.py|
|tests/test_comfy_ui_graph.py|3f465dff8d61ba1e79d7c81e5785231222c7f011|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_comfy_ui_graph.py|
|tests/test_comfy_workflow_metadata_catalog.py|18eb0ff97decbaabe01216fe2bceab6c71c777ac|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_comfy_workflow_metadata_catalog.py|
|tests/test_comfyui_workflow_validation.py|b5c23b8bd49dcd63a3633d3bcd7e7e654793eed2|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_comfyui_workflow_validation.py|
|tests/test_drain_openai_video_jobs.py|7682316ceeb78c84e125c7ce8e738e7d562ecde6|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_drain_openai_video_jobs.py|
|tests/test_gateway_drain.py|824f0440f2136d93938f7af67a6ff31b6918e854|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_gateway_drain.py|
|tests/test_image_output.py|0ed4f2e202bb16fc90e80a37be053af1462349f7|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_image_output.py|
|tests/test_image_polling.py|9e67e10ea975d6b13f1a5e1f5f93355b3b164ce8|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_image_polling.py|
|tests/test_media_contract_validation.py|e687ec3eeb8bf37ceb7c4dfd97997c6f9d2b277f|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_media_contract_validation.py|
|tests/test_media_fetch_security.py|62b60669535892b012cc864a5672d9376fa3dcec|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_media_fetch_security.py|
|tests/test_media_ingestion.py|cbfa7bd5dc92e9ff87c6c9509f360d8b70d3d42f|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_media_ingestion.py|
|tests/test_sync_media_routes.py|13cdbd7656be6497d90ed6a750b6bef8f49d1d0a|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_sync_media_routes.py|
|tests/test_tangoflux_audio_generate_manifest.py|8e0731b5051813789bb612a3e20fc3b7a686a032|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_tangoflux_audio_generate_manifest.py|
|tests/test_uncensored_image_manifests.py|39979e2f3c6e6d40d5aac9f6104710a5fe7231d0|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_uncensored_image_manifests.py|
|tests/test_video_create_sdk_shape.py|8691b562fc616a20188fc872a422302df40e1fd4|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_create_sdk_shape.py|
|tests/test_video_extend.py|c87ceac7616603100a641cc5785f5d64a487fc84|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_extend.py|
|tests/test_video_extend_h3.py|de42481b25c37091a3f495bb0dfb78cdf8422442|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_extend_h3.py|
|tests/test_video_limits.py|5b0be09dec7a2862a4b7fce8a6bf983907eec9ad|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_limits.py|
|tests/test_video_list_delete_remix.py|8d4e921d6a01ba740726c30bd139b438201bd080|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_list_delete_remix.py|
|tests/test_video_ltx25_h3_workflows.py|bcaedf8d47048044fcaebaedc79319a8cb475b83|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_ltx25_h3_workflows.py|
|tests/test_video_media_job_http.py|0bf2e37c955cc739fb26cf07623a351e356bf768|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_media_job_http.py|
|tests/test_video_media_job_integration.py|ae2249b0b4c57915bb70935aa41bbe152b432ba3|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_media_job_integration.py|
|tests/test_video_media_jobs.py|60a4b85e0abfefa5a2124700b1bc33ba7be052b7|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_media_jobs.py|
|tests/test_video_remix_replace.py|a4b467a7dc20b0de9a87886c9d3d2244c02183fd|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_remix_replace.py|
|tests/test_video_subject.py|1347f06d0a409a4c7f86fde9a666b7a2547260f4|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_subject.py|
|tests/test_video_workflow_execution.py|15d4f084a185b6b4d505ea94a2d91562c137f4db|https://github.com/tnunamak/azizam/blob/e35d243907779a8ac19ee8fcef745bb9b32380d7/tests/test_video_workflow_execution.py|

# Workflow execution routes: leaf code-to-data design

Repository: tnunamak/azizam  
Base: c9ffbf84aeb736c45ac984333b2c38c446ee3cc1  
Assignment: c2d-workflow-execution-routes-design-1110  
Status: design only; no source edits, repository tests, publication or deployment performed.

## Recommendation and boundaries

Add an `execution` section to `registry/workflow_request_maps.yml`. Extend its existing reader in `gateway/workflow_request_maps.py` with immutable execution rows and a small, closed renderer called by `route_inputs` and `_execution`. Keep the existing `maps` section unchanged. The new rows contain exact intent keys, ordered extra-field definitions, sync/async wire templates, model prefixes and the optional native alternate. Python must not dispatch on the row name or on an intent spelling.

This is a documentation projection, not a runtime routing migration. The current module explicitly describes itself as pure (`gateway/workflow_openapi.py:7-19,30`), and its catalog calls these two helpers (`:582-605`). Actual frontend selectors already have a different, capability-based purpose (`registry/protocol_frontends.yml:980-986,988-1004,1107-1127`). Do not reinterpret those selectors as this catalog's dispatch table. Protocol `images.generate` is not workflow `image.generate` (`registry/protocol_frontends.yml:134-141`).

The existing request-map file is the narrowest home: it already associates intents and modes with request placement (`registry/workflow_request_maps.yml:20-175`), and its reader already supplies strict keys, version checks, duplicate-selector checks and read-only structures (`gateway/workflow_request_maps.py:49-53,69-94,240-266`). Its top-level accepted keys must deliberately gain `execution`; just appending YAML currently fails (`:50,249-251`). Compatibility data is not the right home: that file describes legacy migrations, aliases and limit keys, not current execution surfaces (`registry/compatibility.yml:1-33`).

Six exact intent rows are evidenced by the allowed implementation and tests: image.generate, image.edit, audio.generate, mesh.generate, video.generate, video.edit. This is not an independently verified exhaustive manifest inventory: manifests and the stored goldens are outside the allow-list. A future implementation must prove completeness against the actual loaded registry, rather than hard-code this six-element set.

## Existing behavior that must survive

All references in this document are to the pinned base.

- Native binding wins before every intent-specific route. It produces multipart sync JSON, no model on the primary route and no async route; only image.edit gets an alternate (`gateway/workflow_openapi.py:321-325,430-438`).
- Model spelling is `video-` plus short id for both video intents, otherwise short id. Full workflow ids are stripped only when they start with the record's actual intent plus a dot (`:87-90,406-408`).
- Extra fields are ordered: image count, missing video duration, missing size. The image-edit count still advertises `in: body`; only its size advertises `in: form`. Do not fix that oddity in a parity change (`:387-400`).
- The size condition is NOT “neither width nor height”: it is “not both settable width and height.” One missing dimension still produces size. The duration condition is “no settable length” (`:382-395`).
- All extras disappear for native routes and when the recommended route is the concrete workflow run route. The latter is recommendation-dependent, not an unconditional video.edit exclusion (`:368-386,489-500`).
- Image generation and audio generation wrap a sync-style request in the durable jobs API. Image edit has no async route (`:412-428,439-456`).
- Mesh async is the same multipart endpoint with Prefer: respond-async, not a JSON job submission. It uses request_mapping(record, "sync"), publishes a GLB content output, and carries a cancellation caveat (`:457-476`). Tests already distinguish these behaviors (`tests/test_workflow_openapi.py:319-354`).
- Video's async API has its own status vocabulary and DELETE cancellation. Video edit additionally advertises a multipart full-id run route whose response is a media job, without changing the upload into a media-job envelope (`gateway/workflow_openapi.py:477-493`; `tests/test_workflow_openapi.py:154-167`).
- Recommendation remains: sync if a sync route exists and a non-None duration is <=30; otherwise async if present; otherwise sync. The duration normalizer excludes booleans/non-numbers and converts numbers to float (`gateway/workflow_openapi.py:52,494-500,565-569`).
- Field maps must still pass through request_mapping, including its container-media exclusion and reference validation. Do not reproduce only the YAML placement pass (`:250-280`).

## Proposed closed data shape

The following is a complete proposed execution section, not a patch to existing source. The existing top-level schema_version: 1 and maps list stay. Mapping/list insertion order below is significant for byte-preserving rendering.

A route template contains the actual published wire dictionary. Exactly four calculated slots exist:
- `{from: model}`: the selected row's model_prefix plus short_id(record), only at model positions.
- `{from: native_path}`: gateway_route(record), only at the native sync path.
- `{from: workflow_run_path}`: the existing full-id run-path construction, only at a sync path.
- `{from: fields, mode: sync|async}`: request_mapping(record, mode), only at the route's fields position.

These are typed slot objects, not executable expressions, arbitrary interpolation, dotted attribute access, imports or a recursive workflow language. All other objects must validate as the closed wire schema of their position. In particular, job_id and video_id braces in literal URLs remain literal; they are not format strings.

Named templates are only deduplication. The reader resolves references by dictionary lookup and validates the resulting object; it never switches on template names.

```yaml
execution:
  field_rows:
    count:
      unless_all_inputs: []
      value:
        name: n
        type: integer
        minimum: 1
        maximum: 10
        label: number of results
        description: Number of images.
        in: body
        field: n
    duration:
      unless_all_inputs: [length]
      value:
        name: seconds
        type: number
        minimum: 1
        description: Clip length in seconds.
        in: body
        field: seconds
    size_body:
      unless_all_inputs: [width, height]
      value:
        name: size
        type: string
        description: WIDTHxHEIGHT, e.g. 960x512.
        in: body
        field: size
    size_form:
      unless_all_inputs: [width, height]
      value:
        name: size
        type: string
        description: WIDTHxHEIGHT, e.g. 960x512.
        in: form
        field: size

  native_sync:
    method: POST
    path: {from: native_path}
    content_type: multipart/form-data
    fields: {from: fields, mode: sync}
    output: JSON (see the route's response schema)
    response: {kind: json}

  sync_templates:
    generated_image:
      method: POST
      path: /v1/images/generations
      content_type: application/json
      model: {from: model}
      fields: {from: fields, mode: sync}
      output: data[].b64_json or data[].url
      response: {kind: json}
    edited_image:
      method: POST
      path: /v1/images/edits
      content_type: multipart/form-data
      model: {from: model}
      fields: {from: fields, mode: sync}
      extensions_field: extensions (JSON string)
      output: data[].b64_json or data[].url
      response: {kind: json}
    generated_audio:
      method: POST
      path: /v1/audio/generate
      content_type: application/json
      model: {from: model}
      fields: {from: fields, mode: sync}
      output: audio bytes (see Content-Type)
      response: {kind: binary}
    binary_model:
      method: POST
      path: /v1/3d/generations/glb
      content_type: multipart/form-data
      fields: {from: fields, mode: sync}
      output: model/gltf-binary (GLB bytes)
      response: {kind: binary, media_type: model/gltf-binary}
    workflow_job:
      method: POST
      path: {from: workflow_run_path}
      content_type: multipart/form-data
      fields: {from: fields, mode: sync}
      output: a media job; its content is the video
      response:
        kind: media_job
        poll: /gateway/v1/media/jobs/{job_id}
        content: /gateway/v1/media/jobs/{job_id}/content

  async_templates:
    generated_image_job:
      kind: media_job
      submit:
        method: POST
        path: /gateway/v1/media/jobs
        content_type: application/json
        body: {intent: image.generate, request: "<the sync request body>"}
        model: {from: model}
      poll: {method: GET, path: "/gateway/v1/media/jobs/{job_id}"}
      content: {method: GET, path: "/gateway/v1/media/jobs/{job_id}/content"}
      cancel: {method: POST, path: "/gateway/v1/media/jobs/{job_id}/cancel"}
      statuses: [queued, running, succeeded, failed, canceled]
      terminal: [succeeded, failed, canceled]
      fields: {from: fields, mode: async}
    generated_audio_job:
      kind: media_job
      submit:
        method: POST
        path: /gateway/v1/media/jobs
        content_type: application/json
        body: {intent: audio.generate, request: "<the sync request body>"}
        model: {from: model}
      poll: {method: GET, path: "/gateway/v1/media/jobs/{job_id}"}
      content: {method: GET, path: "/gateway/v1/media/jobs/{job_id}/content"}
      cancel: {method: POST, path: "/gateway/v1/media/jobs/{job_id}/cancel"}
      statuses: [queued, running, succeeded, failed, canceled]
      terminal: [succeeded, failed, canceled]
      fields: {from: fields, mode: async}
    prefer_binary_model_job:
      kind: respond_async
      submit:
        method: POST
        path: /v1/3d/generations/glb
        content_type: multipart/form-data
        headers: {Prefer: respond-async}
      poll: {method: GET, path: "/gateway/v1/media/jobs/{job_id}"}
      content:
        method: GET
        path: /gateway/v1/media/jobs/{job_id}/content
        output: model/gltf-binary (GLB bytes)
      cancel:
        method: POST
        path: /gateway/v1/media/jobs/{job_id}/cancel
        note: "the engine cannot stop mid-run: the job ends as canceled when the run returns"
      statuses: [queued, running, succeeded, failed, canceled]
      terminal: [succeeded, failed, canceled]
      fields: {from: fields, mode: sync}
      output: "202 with a media job (Location, Preference-Applied: respond-async)"
    video_resource:
      kind: video
      submit:
        method: POST
        path: /v1/videos
        content_type: application/json
        model: {from: model}
      poll: {method: GET, path: "/v1/videos/{video_id}"}
      content: {method: GET, path: "/v1/videos/{video_id}/content"}
      cancel: {method: DELETE, path: "/v1/videos/{video_id}"}
      statuses: [queued, in_progress, completed, failed]
      terminal: [completed, failed]
      fields: {from: fields, mode: async}

  intents:
    image.generate:
      model_prefix: ""
      route_inputs: [count, size_body]
      sync: generated_image
      async: generated_image_job
      native_alternate: null
    image.edit:
      model_prefix: ""
      route_inputs: [count, size_form]
      sync: edited_image
      async: null
      native_alternate:
        method: POST
        path: /v1/images/edits
        model: {from: model}
        note: "options go in extensions['com.azizam.workflow']"
    audio.generate:
      model_prefix: ""
      route_inputs: []
      sync: generated_audio
      async: generated_audio_job
      native_alternate: null
    mesh.generate:
      model_prefix: ""
      route_inputs: [size_body]
      sync: binary_model
      async: prefer_binary_model_job
      native_alternate: null
    video.generate:
      model_prefix: video-
      route_inputs: [duration, size_body]
      sync: null
      async: video_resource
      native_alternate: null
    video.edit:
      model_prefix: video-
      route_inputs: [duration, size_body]
      sync: workflow_job
      async: video_resource
      native_alternate: null
```

Empty unless_all_inputs means unconditional inclusion; a nonempty list suppresses the field exactly when all its names are present in settable_inputs. Do not use Python's all([]) without the nonempty guard. Evidence for every field and template value is `gateway/workflow_openapi.py:387-400,412-493`; the slots reproduce the existing calls at those same lines. Mesh's otherwise surprising size_body candidate follows the generic non-audio size condition at :396-400. A native mesh record still suppresses all candidates under :385-386; this design does not assume whether the shipped manifest has a gateway binding.

No `video.` fallback appears in execution rows. A future video-like intent gets an explicit row referring to video_resource. The existing request-placement prefix at `registry/workflow_request_maps.yml:135-169` is untouched by this leaf; an unfamiliar intent can add or reuse a request-map row too. This removes identity switches from these two helpers, not every remaining selector in augment_openapi. For example, its schema/route augmentation still has literal selection at `gateway/workflow_openapi.py:722,778-790,899`. This design must not claim full unfamiliar-intent coverage of all OpenAPI augmentation.

## Reader, validation and initialization

Proposed API in the existing reader:

- Frozen ExecutionRow(model_prefix, route_inputs, sync, async_, native_alternate).
- Frozen ExecutionMaps(path, field_rows, native_sync, sync_templates, async_templates, intents).
- `execution_row(intent)`: exact lookup, reporting path and missing intent; no default/prefix lookup.
- `validate_execution_records(records)`: validate all distinct intent values from the actual loaded records, including native records; return nothing or raise a path-qualified ValueError.
- `render_sync / render_async / render_alternate`: same closed template renderer with position-specific schemas and the four slots. Return fresh ordinary dict/list values, preserving insertion order.

Extend the existing load once at import pattern (`gateway/workflow_request_maps.py:390-400`, with _MAPS initialized at :390). Do not cause an import cycle: parsing must not import workflow_openapi or discovery; any field rendering callback is passed from workflow_openapi or invoked only at call time. The current module already postpones its workflow_openapi import for this reason (`:272-276`).

### Structural validation at load

The YAML document remains schema_version integer 1 (reject bool/float), with exactly schema_version, maps, execution at top level. The new section is mandatory in the bundled configuration. Keep current maps validation and selector behavior unchanged.

For execution and every nested object, reject unknown keys and missing required keys, with an error like:

`registry/workflow_request_maps.yml: execution.intents.video.edit.async: unknown template 'vidoe_resource'`

Validate:
1. Mapping/list types; nonempty string keys; duplicate YAML keys through the existing safe_load_strict route; duplicate field refs; duplicate output field names in an intent's candidate list.
2. Exact execution keys field_rows, native_sync, sync_templates, async_templates, intents. Each intent requires exactly model_prefix, route_inputs, sync, async, native_alternate. model_prefix may be empty; intent and referenced template names may not.
3. Each field row requires exactly unless_all_inputs and value. Guard list contains unique nonempty input names. Value has only name, type, minimum, maximum, label, description, in, field; name/type/description/in/field required; scalar types and numeric bounds validated, rejecting boolean-as-number; in is body or form. No arbitrary condition language.
4. All template references exist in the correct sync/async namespace. Null sync/async is an intentional absence, not an absent YAML key. Native-only future rows may have both null, but every such record must have a valid native binding during record validation.
5. Closed sync wire keys method, path, content_type, model, fields, extensions_field, output, response; required method/path/content_type/fields/output/response. Native template excludes model and extensions_field. Alternate only method/path/model/note. Method is POST for these submission shapes.
6. Closed async wire keys kind, submit, poll, content, cancel, statuses, terminal, fields, output; all except output required. Submit only method/path/content_type/body/model/headers, with method/path/content_type required. Each endpoint has closed method/path/output/note keys appropriate to its position. A body has exactly intent/request, nonempty strings. Headers are currently a closed Prefer header, not an arbitrary secret-bearing map. Validate resource-relative paths and existing literal job/video placeholders.
7. response is a closed union: json requires kind only; binary adds optional media_type; media_job requires poll/content. This is wire-format validation, not dispatch by domain.
8. kind is the supported wire union media_job/respond_async/video. Branching on this protocol shape for validation is legitimate; no template or intent name lookup is embedded in code. Validate kind-specific structure: media_job requires JSON submit body, respond_async requires Prefer and sync field mode with no wrapper body, video is a polled resource with its specified row data. Validate statuses as unique strings and terminal as a nonempty subset; do not sort either.
9. Slots must have exactly their documented keys, only in their documented positions, and mode must be sync or async. Reject unknown from values, wrong-position slots, from plus extra text, and arbitrary expression/interpolation forms.
10. Freeze nested values recursively, not just the outer dictionary. A render must thaw/copy every list and dictionary including statuses, headers, response, field rows and alternate. No caller may mutate future output or the loaded YAML structures.

The existing loader path-qualifies YAML and schema failures (`gateway/workflow_request_maps.py:240-266`); extend that convention for I/O errors and new nested paths. Source inspection verified its use of safe_load_strict, but that helper's implementation is not in scope, so duplicate-key behavior must have an explicit test rather than be assumed.

### Record completeness and startup

Import-time syntax validation cannot know all loaded workflow intents. After WorkflowRegistry has loaded all records, the startup/discovery owner must call validate_execution_records before publishing routes or marking startup ready. Any missing intent, native or not, fails with the registry filename, intent and affected record ids. Do not derive the required intents from protocol_frontends.yml: those protocol labels differ from workflow intents, and it lacks a complete manifest inventory.

Within the inspected files, call the same validator at the start of workflow_catalog and augment_openapi, before mutating spec or iterating schemas; they already materialize/sort records (`gateway/workflow_openapi.py:582,761`). Revalidate the passed records, not a cached set keyed by an earlier registry. This gives fail-closed publication. It is not by itself proof of startup enforcement: the actual startup call site is outside the allow-list and was not inspected. The implementation must wire the hook in its owning module under separate scope before claiming “fails at startup.”

For direct helper compatibility, native routing may still render the native template with no explicit row, obtaining an alternate only from an optional exact lookup. That preserves the existing standalone unfamiliar.render native test (`tests/test_workflow_openapi.py:625-646`). Production publication/startup validation nevertheless requires an explicit row for every actual registry intent. A nonnative unknown intent always fails exact lookup. Test these two contracts separately, so the native convenience helper cannot silently bypass production completeness.

## Rendering flow and code that remains

_execution(record, typical):
1. Calculate native = gateway_route(record).
2. If native: render native_sync; append alternate only when the record's exact row declares one. async remains None.
3. Otherwise require the exact row; render its sync and async references or None, using the row's model_prefix and the current record. Never select on intent spelling.
4. Apply the existing recommendation function unchanged and return keys in order recommended, sync, async.

route_inputs(record):
1. Obtain current settable_inputs.
2. Retain native/run-route early suppression using gateway_route and _carried_by_run_route, unchanged.
3. Require the exact execution row. For each ordered field ref, skip if its nonempty guard list is entirely settable; otherwise append a fresh copy of value.

Keep these mechanisms in Python:
- Native-route detection and precedence, short-id stripping and full-id run-path construction: record-dependent operations, not facts that should be duplicated per workflow (`gateway/workflow_openapi.py:87-90,321-325,368-372,430-438,489-493`).
- The typical-time recommendation and normalization: depends on the actual record and availability of both routes; preserve None/bool/boundary behavior rather than authoring a preferred mode into every row (`:494-500,565-569`).
- Mapping resolution, media-container exclusion, reference validation, and fresh rendering: actual algorithms with existing contracts (`:250-280`).
- Guard evaluation and extra-field suppression: operations over settable inputs/current recommended route, not intent identity (`:368-386`).
- Enforcement of known transport semantics, including respond_async's unwrapped multipart submit and sync field mapping: these are protocol invariants. The mesh row's literal path, GLB media type, output/cancellation text and choice of respond_async belong in data; no `if intent == "mesh.generate"` is needed to enforce them. The HTTP execution handler itself is unchanged and uninspected.
- Strict loading and deterministic serialization order. No callouts, model APIs or runtime registry fetches are introduced.

Do not move code into per-intent Python handlers, choose a YAML “kind” that is secretly an intent alias, or build a six-entry Python allow-list of allowed intents. An unfamiliar row using existing transport structure must work without editing Python.

## Small leaves and count accounting

There is a count discrepancy that must remain explicit. The brief's 12 is reproducible as twelve identity-based decision expressions in these two functions: four in route_inputs and eight in _execution. It is NOT the same unit as the inspected ratchet. The ratchet counts matching string occurrences/identifiers, then subtracts reviewed allowances (`tests/test_domain_literal_ratchet.py:7-22,106-147,164-210`).

Here are independently reviewable leaves, smallest isolated rewrites before risky transport cases. Every implementation leaf runs the frozen parity suite without recapturing goldens.

| Leaf | Change | Brief's decision sites removed | Visible identity literals removed from those decisions |
|---|---|---:|---:|
| 0 | Freeze existing output and add boundary/mutation tests | 0 | 0 |
| 1 | Load execution data, validate it, compare unused/shadow renderer to old output | 0 | 0 |
| 2 | Read model_prefix instead of video-prefix conditional (:408) | 1 | 1 |
| 3 | Read native_alternate instead of image-edit conditional (:436) | 1 | 1 |
| 4 | Read ordered route_inputs with guards (:387,391,396,399) | 4 | 5 |
| 5 | Switch image generation, image edit and audio generation to generic templates (:439,445,451) | 3 | 3 |
| 6 | Switch mesh sync/respond_async pair as one semantic unit (:457) | 1 | 1 |
| 7 | Switch video async and optional full-id run sync as one unit (:477,489) | 2 | 2 |
| Total | Remove all identity decisions in the two helpers | 12 | 13 |

Leaf 5 also moves two wrapped-intent literals in job_route calls (:444,456) into data, and leaf 7 moves the published literal kind "video" (:479) into data. Thus these functions contain at least 16 obvious matching literal occurrences under the six-intent/video-family vocabulary, even though there are twelve decisions. Other prose/identifiers could match the full vocabulary. No exact before/after **unreviewed global ratchet count** can be claimed from this read scope: manifests, protocol descriptions, reviewed allowances and budgets are not allowed reads, and all_sites also opens the prohibited root module (`tests/test_domain_literal_ratchet.py:49-82,150-160`).

At implementation time, measure the full authorized ratchet before/after each leaf and lower its budget by the measured unreviewed delta, updating stale allowances if removed literal counts require it. Do not raise the budget, create new intent exemptions, or promise that a “12” budget decrement will pass. Those files require additional implementation scope. This review itself removes zero counted sites.

Shadow comparison in leaf 1 is test-only; do not keep a production fallback to the old branches. During incremental leaves, temporary old branches disappear in the final result. The final tests must prove adding a data row, not changing the branch code, changes the projection.

## Proving tests in the existing files

### tests/test_workflow_openapi.py

Retain existing focused assertions: route inputs (:200-209); async semantics, prefix and image-edit absence (:257-272); field ownership (:274-285); mesh form/job/examples (:295-354); duration propagation (:366-381); augmentation idempotence/order (:399-416); sync response kind versus output text (:620-658).

Add pure helper/loader tests, with fixture data supplied through an injectable loaded ExecutionMaps instance:
1. Full exact execution dictionaries for all six rows, not only key presence. Include native=true for each row to prove precedence; only image.edit gets alternate, no native primary model and no native async.
2. Recommendation matrix for None, 0, 29.999, 30, 30.001 and slow values across sync-only, async-only and both. Separately test _typical_seconds for bool/string/int/float; preserve existing handling of NaN/infinity rather than silently adding new restrictions in a parity leaf.
3. Extra fields: zero/one/both settable dimensions; dimension present but settable:false; length present/absent/nonsettable; preexisting n; exact list order; exact descriptions/bounds; image-edit count stays body while size stays form. Native always returns []; video-edit with <=30 and a sync run route returns []; with >30 or None and available async returns ordered duration/size candidates subject to guards.
4. Spy on request_mapping modes: ordinary sync uses sync, media_job/video uses async, respond_async uses sync even though it is under execution.async. Preserve container exclusions and repeated image[] maps.
5. Native override with a synthetic unfamiliar.render and path /native-fixture keeps the existing direct helper test. Separately, validate_execution_records must reject that record until a row is added. Nonnative unknown fails both direct _execution and publication.
6. Data-only acceptance: add a synthetic unfamiliar.render row, arbitrary new model_prefix and route path, plus a compatible maps row. Assert _execution, route_inputs and workflow_catalog follow the supplied row. Then change ONLY its data response kind, alternate, path or a valid guard/placement and assert only the corresponding output changes. A second unfamiliar name using the same template prevents accidental special-casing. This proves the leaf, not augmentation's still-hard-coded route registration.
7. Loader table: unknown top/execution/intent/template/endpoint/response/slot/field keys; duplicate YAML intent key; missing section/key/intent/template; bad mode/slot location/status subset; bool version/bounds; nonmapping document; incompatible transport shape; both routes absent on nonnative records. Assert each error names the actual temporary registry filename and nested field or missing intent.
8. Mutation isolation: mutate returned response, statuses, terminal, submit.headers, alternate, fields and route_inputs; the next render and loaded registry stay unchanged. Render two different records sharing video_resource and prove model/fields do not leak.
9. Missing-intent validation occurs before augment_openapi mutates the supplied bare spec. Reusing a previously validated registry with an added record must fail, not hit a stale coverage cache.
10. Load validation runs once for bundled static rows, but record coverage runs for each actual registry. Runtime startup refusal needs an integration test in the startup owner's permitted scope; pure loader tests alone are insufficient.

### tests/test_c2d_openapi_parity.py

Keep all existing golden comparisons unchanged and do not run the recapture utility:
- Exact workflow set and capture inputs (:41-43).
- Every workflow OpenAPI fragment (:46-48).
- Shared components/info/catalog order (:51-52).
- Exact path set and every path operation (:55-61).
- Catalog set/order/envelope (:64-67), all examples (:70-73) and all catalog rows (:76-79).

These guards already catch broad structural changes across both surfaces. They hash **canonical JSON**, not raw HTTP bytes (:10-15,31-38). Therefore a green existing suite is necessary but cannot alone establish the requested literal byte identity.

For the stronger byte promise, extend this file with a pre-change, frozen baseline from the same deterministic capture inputs and the actual publication serialization options. Compare ordered compact JSON bytes for complete augmented OpenAPI and complete catalog payloads, including example strings. Verify unchanged ordered key sequences when inserting model/fields/alternate and unchanged status/list/record order. If actual HTTP response byte identity is claimed, also freeze responses using the same application/test-client serializer, fixed inputs and headers; exclude volatile data only if it was never part of the stated invariant. Do not substitute sorted-key serialization and call that raw-byte equality.

The exact support-helper API, baseline artifact content and actual route serialization were not inspected, so the implementation must verify how to expose complete payloads before adding this test. This is a required proof step, not evidence already obtained.

Suggested future focused command, only in an authorized implementation environment:
`pytest -q tests/test_workflow_openapi.py tests/test_c2d_openapi_parity.py`
Then run the repository's required checks and ratchet under a scope permitting their reads. No repository test command was run in this review. The proposed YAML in this notes file was parsed locally and its six intent rows were checked for valid template/field references.

## Explicitly not checked

- The prohibited root gateway module, actual startup/discovery wiring and runtime HTTP handler behavior.
- Manifest inventory, whether every shipped mesh record has a native binding, concrete golden contents, capture/support helper implementations, serializer settings, budgets and reviewed ratchet allowances.
- Runtime test results, byte hashes, real or mock service calls, OpenAPI endpoint responses, and global ratchet totals.
- Behavior of every future intent in remaining augment_openapi selectors. This leaf intentionally covers only route_inputs/_execution and shared publication validation.
- No production source or registry file changed. The only created local file is this review; delivery is a sealed bridge artifact for the submitting thread, not a repository commit.


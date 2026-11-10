# Review: simpler execution shape for `registry/workflow_request_maps.yml`

Assignment: `c2d-execution-shape-simpler-muse-1110` — Azizam code-to-data (thread 1f333961).
Repo: `tnunamak/azizam`, base `6e7673175dadb8142695f67ec645456ad0538705`.
Scope read: `registry/workflow_request_maps.yml` only (176 lines, read via the bridge workspace).
Output: this file.

## Verdict

Yes. There is a simpler shape that publishes the same routes: **one row per intent
holding its literal route dictionaries**, with the shared `field_rows` section kept
and the `sync_templates` / `async_templates` sections deleted. Each route keeps
exactly the slots it needs — in practice `model` and `fields`, plus the
record-dependent path slot where the path itself is computed (`native_path` for
the native route, `workflow_run_path` for the video-edit run route).

## Recommended shape (two intents)

Paths are written `ROUTE` exactly as in the proposal; in the real file these are
the literal published paths, unchanged from the proposal. Nothing else below
changes value — every route dictionary is moved verbatim out of its template.

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

  # Selected by record (gateway_route set), not by intent — unchanged, standalone.
  native_sync:
    method: POST
    path: {from: native_path}
    content_type: multipart/form-data
    fields: {from: fields, mode: sync}
    output: JSON (see the route's response schema)
    response: {kind: json}

  intents:
    image.generate:
      model_prefix: ""
      route_inputs: [count, size_body]
      sync:
        method: POST
        path: ROUTE
        content_type: application/json
        model: {from: model}
        fields: {from: fields, mode: sync}
        output: data[].b64_json or data[].url
        response: {kind: json}
      async:
        kind: media_job
        submit:
          method: POST
          path: ROUTE
          content_type: application/json
          body: {intent: image.generate, request: "<the sync request body>"}
          model: {from: model}
        poll: {method: GET, path: "ROUTE"}
        content: {method: GET, path: "ROUTE"}
        cancel: {method: POST, path: "ROUTE"}
        statuses: [queued, running, succeeded, failed, canceled]
        terminal: [succeeded, failed, canceled]
        fields: {from: fields, mode: async}
      native_alternate: null

    video.edit:
      model_prefix: video-
      route_inputs: [duration, size_body]
      sync:
        method: POST
        path: {from: workflow_run_path}
        content_type: multipart/form-data
        fields: {from: fields, mode: sync}
        output: a media job; its content is the video
        response:
          kind: media_job
          poll: ROUTE
          content: ROUTE
      async:
        kind: video
        submit:
          method: POST
          path: ROUTE
          content_type: application/json
          model: {from: model}
        poll: {method: GET, path: "ROUTE"}
        content: {method: GET, path: "ROUTE"}
        cancel: {method: DELETE, path: "ROUTE"}
        statuses: [queued, in_progress, completed, failed]
        terminal: [completed, failed]
        fields: {from: fields, mode: async}
      native_alternate: null
```

The other four intents follow the same pattern (`audio.generate`,
`image.edit`, `mesh.generate`, `video.generate`); each takes its template
dictionary verbatim. The only template used twice, `video_resource`, appears
once under `video.generate` async and once under `video.edit` async — that
single duplication is the whole cost of deleting the template layer.

## What to cut, and why

Cut the `sync_templates` and `async_templates` sections and their name lookups,
because eight of the nine templates are referenced exactly once so the names buy
no deduplication, and the single twice-used template (`video_resource`) costs
one short duplication while the indirection costs a resolution layer and a whole
class of dangling-name errors.

## Why this publishes the same routes

- **Template references, counted from the proposal:** `generated_image`,
  `edited_image`, `generated_audio`, `binary_model`, `workflow_job`,
  `generated_image_job`, `generated_audio_job`, `prefer_binary_model_job` are
  each referenced once; only `video_resource` is referenced twice
  (`video.generate` async and `video.edit` async). Naming a dictionary that is
  used once is pure indirection — the brief itself states the reader never
  switches on template names, so names carry no meaning.
- **Field rows are the one indirection that pays, so they stay shared:**
  `size_body` is referenced 4 times, `count` and `duration` twice each,
  `size_form` once — 9 references to 4 small uniform records. Inlining them
  would copy `size_body` four times and scatter the `unless_all_inputs` rules.
  The alternative I considered and rejected was inlining the field rows too.
- **Slots are unchanged:** the four slot kinds still exist, and each route
  carries only the ones it needs — `model` + `fields` on the gateway routes,
  `native_path` on the standalone native route, `workflow_run_path` on the
  video-edit run route. The `fields` slot still reproduces `request_mapping`
  from the existing `maps` list; the execution section does not re-describe
  field placement.
- **The proposal already inlines the one-off:** `image.edit`'s
  `native_alternate` is a literal dictionary in the intent row, not a named
  template. The recommendation extends that existing pattern to all routes.
- **Cross-reference gets shorter, not longer:** the async submit body's
  `"<the sync request body>"` now refers to the `sync` dictionary in the same
  intent row instead of a template in another section.
- **`native_sync` stays standalone:** no intent row references it, and the
  existing maps select the native row by record (`native: true`,
  `registry/workflow_request_maps.yml:24-32`), not by intent — so it does not
  belong in any intent row. Its applicability rule lives in the reader, as in
  the proposal; this shape changes nothing there.
- **No `video.` fallback is introduced:** both video intents name their async
  route explicitly, and the existing request-placement prefix
  (`registry/workflow_request_maps.yml:135-169`) is untouched.

## Evidence against the in-scope file

All line numbers are `registry/workflow_request_maps.yml` at the pinned base.

- `image.generate` request placement — prompt in body, `width`/`height`
  combined into body field `size`, route-managed image inputs unset, rest as
  extension keys: lines 60-80. Consistent with the `size_body` row (`in: body`,
  `field: size`) and the sync template's `application/json` body.
- `image.edit` request placement — route pictures first, prompt in form,
  `source_image` as file part `image[]`, `mask` as file part, `width`/`height`
  combined into form field `size`, managed inputs unset, later assets as
  file+extension, rest as extension keys: lines 85-118. Consistent with the
  `size_form` row (`in: form`, `field: size`) and the `edited_image`
  `multipart/form-data` template.
- `audio.generate` — route-managed audio fields at top level, rest as extension
  keys: lines 122-130. Consistent with the `generated_audio` template.
- `video.` prefix — prompt, negative prompt, seed, image strength in body;
  `width`/`height` combined into body field `size`; `length` in body field
  `seconds` (lines 158-160); `image` in body field `input_reference`
  (lines 161-163); route-managed video inputs unset; rest as extension keys:
  lines 135-169. Consistent with the `duration` row (`in: body`,
  `field: seconds`) and the `size_body` row.
- `video.edit` sync is the run route, plain multipart form: lines 49-55
  (comment at lines 48, 132-134: "sync video.edit is the run route above").
  Consistent with `workflow_job` (`multipart/form-data`, `fields` sync) and
  with `video.generate` having no sync route (`sync: null` — no
  `video.generate` sync row exists in the file).
- `mesh.generate` sync — inputs no service binding maps are not form fields,
  assets are file parts, default form: lines 36-46. Consistent with the
  `binary_model` template (`multipart/form-data`, no `model` key).
- Native sync — plain multipart, `source_image` renamed to `image`, each asset
  a file part, every other input a form field: lines 24-32. Consistent with
  `native_sync`.
- Fallback for any other intent — a key under
  `extensions["com.azizam.workflow"]`: lines 172-175. Consistent with the
  `image.edit` `native_alternate` note, "options go in
  `extensions['com.azizam.workflow']`".
- Placement order (`inputs`, `sets`, `asset`, `default`; first matching part
  wins) and set resolution by word are documented in the file header:
  lines 1-17. The `fields` slot relies on exactly this machinery, which is
  why the execution section needs no placement data of its own.

## One tension found, not resolved

- The `count` field row says `n` is a body field (`in: body`, `field: n`).
  In the in-scope file, neither image intent places `n`: `image.generate`
  (lines 60-80) and `image.edit` (lines 85-118) both send unlisted inputs to
  `extension` by default, so `request_mapping` would place `n` under
  extensions, not in the body. Either the field rows describe a different
  layer than `request_mapping` placement (the workflow route's own published
  schema, where `n` is a body parameter the workflow layer then maps), or the
  row is wrong. The brief cites the workflow OpenAPI module lines 387-400 for
  the row; that citation is out of scope and unverified here — see below.

## What could not be checked

- **The workflow OpenAPI module.** Every field and template value in the
  proposal is evidenced in the brief by citations to that module
  (lines 385-386, 387-400, 396-400, 412-493, 722, 778-790, 899). It is outside
  this assignment's read allow-list — only
  `registry/workflow_request_maps.yml` was in scope — and the brief forbids
  opening the large root gateway module. All correspondence between the
  proposed values and the existing code is therefore taken from the brief's
  citations, not independently verified, including the mesh `size_body`
  candidate, the `count` row above, and the claim that literal selection
  remains at lines 722, 778-790 and 899.
- **The literal route paths.** Written `ROUTE` in the brief; they do not appear
  in `registry/workflow_request_maps.yml`.
- **Reader/loader behaviour.** Dictionary lookup, validation of the resolved
  object, byte-preserving insertion order, and `unless_all_inputs` evaluation
  live in `gateway/workflow_request_maps.py` and the OpenAPI module, both out
  of scope; the statements above about what the reader does are the brief's,
  not verified here.
- **The other two assignments in this batch**
  (`c2d-image-defaults-required-muse-1110`,
  `c2d-video-defaults-required-muse-1110`) were already claimed by another run
  of this worker (`claim_busy`) and were not worked here.

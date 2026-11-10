# CODE_TO_DATA S7: the video move, four steps against frozen interfaces

Status: design, docs only. Base `origin/build/c2d-v3c-wip` (2c7db80fe). Parent: `CODE_TO_DATA_S7_PARITY.md` (M7, M8, M9, section 4
deltas). Inventories: `c2d-s7-parity/video.md` (ids `video-NN`) and `writers.md`. Last attempt: builder V3c, which stopped because
create could not move without the facts below.

The goal: `POST /v1/videos`, `/v1/videos/{id}/remix`, `/v1/videos/edits` and the four lifecycle routes become rows served from the
job store, with the wire (status, error text, headers, engine requests, receipts, asset rows) unchanged. Four builders (V5a to V5d)
work in parallel. Each section below freezes the names, types and behaviour the other three rely on. A builder may add private
helpers. A builder may not change a frozen name; it asks the design owner (this document's author) for a revision.

## 0. Scope, and the one thing this document narrows

In scope: single-prompt create (t2v, i2v, keyframes, subject-select replace), remix, edit, status, content, list, delete, the
orphan rule, create gates, receipts, History, tests.

**Narrowed: chain graphs (`shots`, `seconds` past a model's native length, `POST /v1/videos/extensions`) keep running on the
legacy handler in V5.** M9's graph templates and the `compose: segment_chain` composer do not exist in the tree
(`registry/protocols/` has no such protocol; `gateway/video_extend.py:182` and `gateway/video_extend_h3.py:124` still build the
graphs in code). Moving chain create before M9 would copy the two builders into the shape. So V5 freezes chain *selection* as data
(section 2.4) and a handoff, and a chain request creates a legacy `video_<prompt id>` record, which the M7 legacy reader serves.
What V5 does move for chains: the *source* of an extension may be a new-style video (section 4.6). The handoff has a ratchet and
retires with M9 (V6). This is the only point where V5 does less than the brief's "extend sources" wording suggests; it is a
choice for the design owner, flagged in the report.

Decisions fixed by the brief (not reopened): size, length and frame-grid facts are manifest data; the facts a video object shows are
recorded on the job at create in one named place; the shape answers the old route's validation status and text; create gates are row
data or shape behaviour; parity tests are rewritten for `video_<job id>`; list merges old records and job views under one cursor.

## 1. Shared vocabulary (all four steps use these exact names)

### 1.1 Ids

- Job id: `mj_<32 hex>` (`gateway/media_jobs.py:169`). Wire id: `video_<job id>` (`OpenAIVideoCreate.wire_id`,
  `gateway/frontend_shapes/shapes.py:508`). Legacy wire id: `video_<ComfyUI prompt id>` (`proxy.py:11041`). The two id spaces are
  disjoint (a prompt id is a UUID with dashes, no `mj_`), so lookup tries the job store first and the legacy reader second, as
  `videos_status` already does (`proxy.py:27328`). Malformed id: 400 `invalid video_id` (video-65, video-122). Unknown or not the
  caller's: 404 `video_id not found` (video-64, video-65).
- Prompt-memory key of a new video: `video_<job id>` (the old key was `video_<prompt id>`, `proxy.py:11826`).

### 1.2 Request facts (the frozen record, `RequestFacts`)

Recorded on the job at create, read back by status, list, content and by remix/extend as a source. One new field on `RunRequest`
and one new accessor on `JobService`; both additive.

```python
# gateway/workflow_runtime_types.py (V5b owns the edit; V5c fills it)
@dataclass(frozen=True)
class RunRequest:
    ...
    facts: Mapping[str, Any] = field(default_factory=dict)   # NEW. JSON-safe, no prompt text, no person id. Not run identity.

class JobService(Protocol):
    ...
    def request_facts(self, job_id: str, caller: CallerContext) -> RequestFacts | None: ...   # NEW. None: JOB_NOT_FOUND
```

Storage: `facts` is written once, in the same transaction that inserts the job, to the job's metadata under the key
`request_facts` (next to `result_metadata`, `gateway/workflow_run.py:64`; the write sits in `prepare` at `workflow_run.py:646`).
It is immutable: no later patch touches it. `RunJobView` does not carry it, and `job_object` (`frontend_shapes/_base.py:80`) and the
generic `/gateway/v1/media/jobs` routes do not render it. Only the video shape asks for it through `request_facts`.

`RequestFacts` is a frozen mapping with exactly these keys (a key not listed is refused at write; a key with no value is absent):

| key | type | meaning | old home |
|---|---|---|---|
| `model` | str | the model name the caller sent, or the default | `public_model`, `proxy.py:11782` |
| `resolved_model` | str | the manifest id that ran, with the intent prefix removed (`ltx-2.5-t2v`) | `resolved_model` |
| `size` | str `WxH` | the size the run used, after clamping | `size` |
| `seconds` | str | the seconds the run used, as the old string (`"5"`, `"3.5"`) | `seconds` |
| `actual_seconds` | float | frames divided by frame rate | `actual_seconds` |
| `length_frames` | int | the frame count sent to the engine | provenance parameters |
| `frame_rate` | number | frames per second sent | `frame_rate` |
| `seed` | int | the seed the run used | provenance |
| `audio` | bool | `false` when the caller asked for a silent clip; absent otherwise | `audio` |
| `metadata_mode` | str | the delivery control chosen at submit (`none`, `full`, ...) | `metadata_mode` |
| `provenance` | object | the record `provenance.build_provenance_record` builds (no prompt) | `provenance` |
| `remixed_from_video_id` | str | wire id of the source | same name |
| `extended_from_video_id` | str | wire id of the source | same name |
| `source_asset_id` | str | the Library asset the source video resolved to | new (section 4.6) |
| `total_seconds` | float | chain total (legacy chains only; never written by V5) | same name |
| `warnings` | list[str] | wire warnings, as `_video_job_warnings` (`proxy.py:8190`) | `warnings` |

Not in `request_facts` (private; decision 2):

- **prompt text**: stays in the job payload (`workflow_run.py:646`, prompt-memory rules decide whether it reaches disk) and in prompt
  memory under `video_<job id>`. `RequestFacts` has a read-only property `prompt` that is filled by `request_facts` **only** when the
  caller's person id equals the maker's and `prompt_memory.get("video_<job id>", person)` still returns a row. This is the rule
  `_tracked_video_fields` applies today (`proxy.py:10393`). The generic job view never sees it.
- **person id and owner key**: the job store already has maker and owner (`media_jobs.create(..., maker_from_request=True)`,
  `workflow_run.py:~620`; `can_read`). `request_facts` returns `maker_person_id` only to the maker itself; for any other reader it is
  None, so the content route's `prompt_not_accessible` (403, `proxy.py:13273`) can be decided without leaking the id.
- **api key name, app id**: not recorded; the job store's maker already holds them.

### 1.3 Progress

`RunJobView` gains `progress: int = 0` (0 to 100), read from the job row column `progress` (`gateway/media_jobs.py:343`, which the
runner already writes: `workflow_run.py:757`, `:793`). The video status body uses `view.progress`; the hard-coded 0/100 at
`shapes.py:521` goes. Monotonic: the runner writes `max(previous, new)` (video-60). V5b owns this. The runner's engine-progress
reports (queue-only poll = 25, sampler restarts) reach the row through `ctx.record_progress(pct)` (new, V5b, on `RunContext`,
`workflow_run.py:~201`), called from the poll loop in `graph_round.run_round`.

### 1.4 Validation vocabulary

The old create gives 400 with the text and `param` the old code wrote. The shape answers those, not the run core's 422 (decision 3).
Rule: every refusal that `_proxy_graph_video_create`, `_submit_video_remix` and the form reader make **before** the first engine
request is produced in the shape (`to_run_request` / `read_wire` / `precheck` / `postcheck`) as `_wire.error_response(400, <old
text>, param=..., code=...)`. V5d's text table (section 5.3) is the oracle; V5c copies each sentence byte for byte. The core's
422 `the run request is not valid` is reachable only for a defect the shape cannot see (a manifest bug); that case stays 422.

## 2. V5a: manifest data

### 2.1 What the old code reads from `videos.workflows`

`registry/defaults/videos.yml` and `proxy.py:10810` to `:10945`. Per workflow: `file`, `node_roles` (`prompt_node`,
`negative_prompt_node`, `size_node` (pair or single), `width_node`/`height_node`, `length_nodes`/`length_node`, `frame_rate_nodes`,
`seed_nodes`, `save_node`, `image_node`, `image_strength_node`, `image_strength_default`, `steps_node`, `prompt_field`, the
subject-select node ids), `min_seconds`, `max_seconds`, `default_seconds`, `max_size`, `default_size`, `size_multiple`, `frame_rate`,
`length_step`, `length_offset`, `max_length`, `i2v_workflow`, `chain_workflow`, `builder`, `requires_input_reference`,
`requires_extensions`, `source_video_input`, `subject_select`, `chain_segment_seconds`. Everything that names a graph node is
already in the manifest's `bindings.comfyui.map` (`manifests/workflows/video-ltx-2.5-t2v.yml:55`); what is missing is **which input
carries which meaning** and the **limits**.

### 2.2 The new top-level manifest key: `video`

`registry/workflow.schema.json` has a closed top-level property list (`schema_version` ... `bindings`); V5a adds one optional
property, `video`, valid only when `capability: video`. Types and meaning:

```yaml
video:                                  # object, optional; absent on a non-video manifest
  roles:                                # object: role name -> input name (an input declared in `inputs`)
    width: width                        #   int input receiving the output width in pixels
    height: height                      #   int input receiving the output height in pixels
    length: length                      #   int input receiving the frame count (the grid below decides its value)
    frame_rate: frame_rate              #   number input receiving frames per second (optional role)
    image: input_reference              #   media input (image/*) receiving the first frame (optional role)
    image_strength: image_strength      #   number input, 0..1 (optional role)
    source: source_video                #   media input (video/*) receiving a source clip (optional role)
    last_image: ...                     #   optional; keyframes variants
  size:
    default: 960x512                    # WxH string; used when the request has no size
    max: 1280x704                       # WxH; a larger request is clamped, not refused (video-07)
    multiple: 64                        # int >= 1; sizes are floored to it, with a floor of 64 (`_parse_size_clamped`, proxy.py:10913)
  length:
    min_seconds: 1                      # number; below it is refused with the old text (section 3.3 of V5d's table)
    max_seconds: 10                     # number; a request above it selects the `long` variant or is refused (2.4)
    default_seconds: 4                  # number, optional; used when `seconds` is absent
  grid:                                 # the frame-grid rule, section 2.3
    fps: 24                             # number > 0; the frame rate when the request sends none
    step: 8                             # int >= 1
    offset: 1                           # int >= 0
    min_frames: 9                       # int, optional; floor after rounding
    max_frames: 81                      # int, optional; ceiling (video-scail-2-replace: `max_length: 81`)
  variants:                             # selection, section 2.4; each value is a manifest id
    image: video.generate.ltx-2.5-i2v
    long:  video.generate.ltx-2.5-long
  requires: []                          # subset of [image, source, extensions]; what a request must supply, section 2.4
  select_subject: false                 # true: the subject-select pre-pass runs (video-09, video-116 to video-120)
  chain: false                          # true: this manifest is a chain workflow (handoff, 2.4)
```

`inputs.<role input>` keeps its own `type`, `minimum`, `maximum`, `required`; the role names it. A role input needs a matching
`bindings.comfyui.map` entry. The inputs `width`, `height` and `length` are ordinary inputs, filled by the shape from `size` and
`seconds` (as the image shape fills `width`/`height` by name, `shapes.py:97`, `_wire.declared`). A caller's `extensions` override of
them is refused like any other input the shape owns (text copied from the old refusal, video-06).

### 2.3 The frame-grid rule (frozen)

Given `seconds` (number), `fps` (request `frame_rate` or `grid.fps`), `grid.step`, `grid.offset`:

```
frames = max(1, round(seconds * fps))
k      = max(1, round((frames - offset) / step))
length = k * step + offset
length = clamp(length, grid.min_frames, grid.max_frames)      # only the bounds that are declared
```

This is `_seconds_to_length` (`proxy.py:10936`) exactly, with Python's `round` (banker's rounding on ties). The shape computes it;
there is no second copy: `_seconds_to_length` moves to `gateway/video_grid.py` (V5a owns it), and `proxy.py` imports it until the
old create is deleted (V5c). `actual_seconds = length / fps`; the wire shows `X-Duration-Adjusted` exactly when the old code did
(`length / fps != seconds`; V5c keeps the header from `result_headers` data, V5d pins it). LTX: step 8, offset 1. MiniMax H3: step 17,
offset 5 (`videos.yml:50`). SCAIL-2: step 4, offset 1, max_frames 81. Size: `WxH` (anything else, `auto` included, reads as 1024x1024, as `parse_size` always did), each side cut to `max` first, then
`max(64, floor(side / multiple) * multiple)`, so a `max` that is not a multiple is floored too (`_parse_size_clamped`, main
proxy.py:10913; pinned by `test_size_auto_reads_as_the_old_route_read_it_and_the_clamp_comes_before_the_floor`); the used size is recorded in `size` and the
adjusted note `size_adjusted` is a result header as for images (`result_headers: {size_adjusted: X-Azizam-Size-Adjusted}`).

Loader checks (V5a, `gateway/workflow_loader`/kernel, the `template_role_at_load` delta): every `roles.*` input exists; `width`,
`height` and `length` are required when `video.size` / `video.length` are present; each role input is bound in
`bindings.comfyui.map`; `variants.*` ids exist in the registry snapshot *when* the snapshot is complete (a missing target is a
warning in the registry report, not a load failure, so a deployment may omit the long manifests); `step >= 1`; `min_seconds <=
max_seconds`; `size.default` is within `size.max`. A failure is a registry-load problem (the manifest is marked invalid with the
reason, never served), not a request-time 400.

### 2.4 Selection as data

The old code picks the workflow in this order (`proxy.py:12000` to `:12060`): resolve `model` (alias table, then name) to a
workflow; if `input_reference` is sent and the workflow has `i2v_workflow`, switch to it; if the workflow `requires_input_reference`
and none was sent, refuse; if `shots` is sent or `seconds > max_seconds` and the workflow has a `chain_workflow`, take the chain;
if the target `builder` is a chain engine, it is the chain. The new rule, in the shape's `refine_target` (`_base.py:222`):

1. `model` names a manifest (id, short id or an entry in the model-alias table that already ships). Unknown: 404-class
   `model_not_found` with the old text `Model '<m>' is not available for video generation.` (V5d table).
2. If the request supplies `input_reference` and `video.variants.image` exists: target = that id.
3. If the target's `video.requires` contains `image` and no `input_reference` was sent: 400, param `input_reference`, the old text
   `Model <short id> is image-to-video and requires input_reference`.
4. If `shots` is present, or `seconds > video.length.max_seconds` and `video.variants.long` exists: target = `variants.long`, and
   the request is a **chain handoff** (section 4.5). If the target has no `variants.long`: 400 `long_video_not_supported` with the
   old text.
5. `video.requires: [source]` (SCAIL-2): `extensions.source_video` or an upload must be present; otherwise the old
   `requires_extensions` refusal text (video-53 family).
6. `video.select_subject: true` runs `gateway/video_subject.py` (`validate_selection`, `apply_point`, `warnings_for`) as a
   pre-engine stage; code stays code (it is node-graph geometry, M9's "stays code" list), but the *switch* is manifest data
   instead of `workflow_cfg.get("subject_select")` (`proxy.py:12179`, `:12542`).

`requires_extensions` is `video.requires: [extensions]`; `source_video_input` is `video.roles.source`; `chain_segment_seconds`,
`max_total_seconds` and `allow_untracked_ids` are deployment limits and stay in `videos.yml` (decision 1).

### 2.5 Manifests to change

All eleven under `manifests/workflows/`: `video-ltx-2.5-t2v`, `-i2v`, `-keyframes`, `-keyframes-mid`, `-long`;
`video-minimax-h3-t2v`, `-i2v`, `-keyframes`, `-keyframes-mid`, `-long`; `video-scail-2-replace`. For each: add `video:`; add the
inputs the roles need (`width`, `height`, `length`; `input_reference` and `image_strength` on i2v and keyframes; `source_video` on
scail-2) and their `bindings.comfyui.map` entries copied from the node ids in `videos.yml` (`size_node: ["20","21"]` becomes
`width -> node 20 input width`, `height -> node 21 input height`, `length -> length_nodes`). Defaults and limits are copied from the
same file, value for value. The long manifests are `video.chain: true`, carry only the limits and no `roles` they cannot use (their
graph is built by the legacy builder until M9). A test (V5a) proves each value in `video:` equals the `videos.yml` value it came
from, so the retirement of the `videos.workflows` section (CONFIG_HOMES) cannot lose a number.

### 2.6 V5a files, tests, order

- Owns: `manifests/workflows/video-*.yml`, `registry/workflow.schema.json`, the manifest loader and kernel files that validate a
  contract (`gateway/workflow_kernel.py`, the loader module that reads `schema_version: 2`), new `gateway/video_grid.py`,
  new `tests/test_video_manifest_data.py`, `registry/defaults/videos.yml` (only to delete nothing yet; values stay until CONFIG_HOMES
  retires the section).
- Must not touch: `proxy.py`, `gateway/frontend_shapes/*`, `gateway/workflow_run.py`, `gateway/workflow_runtime_types.py`,
  `registry/protocol_frontends.yml`, any test under `tests/test_s7_parity_video_*.py`.
- Must turn green: `tests/test_video_manifest_data.py` (new: schema accepts the key; loader checks 2.3; table equality with
  `videos.yml`; grid examples below), `tests/test_video_limits.py` and `tests/test_video_ltx25_h3_workflows.py` unchanged (they
  call `_seconds_to_length` and `_parse_size_clamped` through `proxy`; the re-export keeps them green), the manifest bundle tests
  that already exist (`tests/test_frontend_*` shipped-manifests checks), the engine boundary guard
  (`tests/test_engine_knowledge_boundary.py`).
- Grid examples V5a pins and V5c reuses: LTX 24 fps, 3 s -> 73 frames (3.0417 s, the example in the `_seconds_to_length`
  docstring, `proxy.py:10936`); H3 and SCAIL-2 examples are computed by V5a from the formula in 2.3 and checked against the
  existing `test_video_limits.py` cases; SCAIL-2 is capped at 81.
- Order: first of the four in time only to the extent V5c's *tests* need the keys; V5c and V5d can start at once because the key
  names, types and the grid formula are frozen here. V5a delivers by merging to the int branch; until then V5c's shape reads
  `video` through the contract accessor `video_facts(entry)` (a tiny function in `gateway/video_grid.py` that returns a typed
  `VideoManifest` from `entry.contract["video"]`), and tests use an in-memory snapshot built from the same dict.

## 3. V5b: job facts and lifecycle reads

### 3.1 What V5b adds (frozen names)

| name | where | meaning |
|---|---|---|
| `RunRequest.facts`, `JobService.request_facts`, `RunJobView.progress` | `workflow_runtime_types.py` | section 1.2, 1.3 |
| `JobService.list(..., after_key)` | `workflow_runtime_types.py:551`, impl `workflow_run.py:1118` | adds `after_key: tuple[int, str] \| None = None`: the page starts strictly after that `(created_at, job_id)` in the requested order. Exactly one of `after_id` / `after_key`; both raises ValueError. A cursor row is not required to exist (this is how a legacy boundary is used). |
| `class VideoViews` | new `gateway/frontend_shapes/video_views.py` | the lifecycle half of `OpenAIVideoCreate` (`wire_id`, `job_id_of`, `invalid_id`, `unknown_id`, `status_body`, `render_status`, `render_listing`, `render_deleted`, `render_delete_conflict`, `render_bytes`), moved out of `shapes.py:498` to `:540`. `OpenAIVideoCreate(VideoViews, Shape)` is the only edit V5b makes to `shapes.py`. |
| `class VideoLedger` | new `gateway/video_ledger.py` | the merged reader: `lookup(wire_id, caller) -> JobHit \| LegacyHit \| None`, `page(caller, limit, after, order) -> ListPage`, `delete(wire_id, caller)`. The handlers at `proxy.py:27314` to `:27395` call it and nothing else. |
| `graph_round.ORPHANED` | `gateway/engine_protocol/graph_round.py:29` | a round failure kind, 3.6 |
| `ctx.record_progress` | `workflow_run.py` `RunContext` | 1.3 |

### 3.2 Status

`VideoViews.status_body(view, facts)` returns the old body (`_openai_video_payload`, `proxy.py:10425`): `id`, `object: video`,
`azizam_object: video.generation`, `status` (wire word: `queued`, `in_progress`, `completed`, `failed`; `canceled` is `failed`, as
today at `shapes.py:488`), `azizam_status` (job-store word, `canceled` shown as `failed`), `progress` (`view.progress`; 100 when
succeeded), `created_at`, `completed_at` (set when terminal, `view.updated_at_s`), `expires_at` (`created_at + 86400`, the old
record value), `updated_at`, `prompt` (facts.prompt, or None), `remixed_from_video_id`, `extended_from_video_id`, `model`, `size`,
`seconds`, `actual_seconds`, `azizam_resolved_model`, `error` (`{code, message}` from `view.failure`), `warnings` when present,
`segments_total` and `segments_completed` only for legacy chain records. Failed text is the job's failure text, which is the
adapter's message (`video-23`, `row.job_failure_text: adapter`). A body key with no value is absent only where the old one omitted it
(the `None` placeholders above are kept, `proxy.py:10442`).

The status route has no engine call. A job's status moves only when its runner polls the engine (`graph_round.run_round`); the
reconcile loop never touched job views. V5d's rig helper (section 5.2) exists because of this.

### 3.3 Content (frozen behaviour)

Order, as `proxy_graph_video_content` (`proxy.py:13136`):

1. 503 `Video generation not enabled` when `DERIVED.COMFY_ENABLED` is false (unchanged, in the thin handler).
2. Not a job of the caller: 404. Not succeeded: 404 `Video content is not ready yet (status=<wire word>)`, code `video_not_ready`,
   type `invalid_request_error` (`shapes.py:531` today uses a different type; V5b makes it `invalid_request_error`, video-70).
3. One result with `media_type video/*`; otherwise 500 `No video output found in job` (type `api_error`).
4. **Result processing before store (adapter side).** The audio strip (`record.audio is False` -> `media_metadata.strip_audio`,
   `proxy.py:13256`) and the provenance stamp (`media_delivery.attach_provenance(bytes, ext, facts.provenance)`, `:13313`) become
   `result_processing` stages of the node-graph binding: `strip_audio` and `attach_provenance`, added to the `result_processing`
   enum (`registry/protocols/node-graph.yml:33`). A stage runs when the run's `options` say so (`options.audio == false`; the
   provenance stage reads `options.provenance_ref`, see 4.3). V5b owns the two stage functions and the enum values; the binding
   data that lists them is in the video manifests and is added by V5a (key `bindings.comfyui.result_processing`), values frozen
   here. The Library copy and the delivered file therefore agree, as the old comment at `proxy.py:13245` requires.
5. **Read-time delivery (shape side).** `metadata_mode` is the request's (`?metadata=` query, `_note_media_metadata_query`,
   `proxy.py:27352`) falling back to `facts.metadata_mode` (the submit-time choice, `:27358`). `none` -> `media_delivery.deliver_bytes`
   strips metadata; `full` requires `facts.maker_person_id == caller person` and a kept prompt and not `household.never_keep`, else
   403 `prompt_not_accessible` with the old message (`:13273`). Headers: `Content-Disposition: inline; filename="<engine file
   name>"` (the file name is `result_metadata["filename"]`, recorded by the adapter; V5b adds that key), media type from the
   extension (`.mp4 video/mp4`, `.webm`, `.mov video/quicktime`, `.mkv video/x-matroska`, else `application/octet-stream`).
6. First fetch indexes the Library asset (`media_ingestion.index_video_delivery`, `:13320`): for a job the asset already exists
   (the adapter committed the bundle), so the index call keys on `video_id = video_<job id>`, `asset_id` from the result ref, and
   `record = facts` (V5b keeps the function's signature; V5d pins the asset row, video-18, video-73).
7. Failures: view/fetch errors keep their texts: 502 `video_failed`, 502 `<engine> /view error: <status>`, 502 `Could not
   prepare video for delivery`, `_VideoAssetError` -> `_video_asset_error_response` (video-75).

Cancel during fetch stores no asset (video-72): the job is canceled before the adapter commits; nothing in V5b stores bytes after
`open_result` returns.

### 3.4 List: the merged cursor (frozen)

`GET /v1/videos?limit=&after=&order=`. Parameters, defaults and errors are the old ones (`proxy.py:13365`; limit default 20, max
100; bad `limit`, `order`, `after` -> 400 with the old texts; copy from `test_video_list_delete_remix.py`).

Order key of every item, in both stores: `(created_at, raw_id)`, `raw_id` being the job id or the prompt id, compared as strings;
`order=desc` reverses both parts (newest first, ties by larger id first). Sources: `JobService.list(caller, intent="video.generate",
...)` (note: the intent filter is the row's `runtime_policy.intent`; remix and edit rows file under `video.remix`, so the list asks
for both intents; V5b adds `intents: Sequence[str]` to `JobService.list`, replacing `intent: str`, the one existing caller being
the video ledger; the tests at `tests/test_workflow_run.py` for list change in the same commit) and the legacy records visible to
the caller and not tombstoned (`_video_job_visible`, `proxy.py:10261`).

Algorithm (`VideoLedger.page`):

1. Resolve `after` (a wire id). `video_mj_...` -> `JobService.get`; if absent for the caller -> the old evicted-cursor error
   (video-79). `video_<prompt id>` -> the legacy record (in memory after hydrate); absent -> the same error. This yields a boundary
   `B = (created_at, raw_id)`. No `after` -> no boundary.
2. Ask the job store for `limit + 1` items strictly past `B` (`after_key=B`), and take from the legacy list the `limit + 1` items
   strictly past `B`. Merge by the order key; take `limit`; `has_more` is true when an item remained.
3. Render each item with the status body of its own store. `first_id` / `last_id` / `object: list` as today (`shapes.py:535`).

`after` is therefore the wire id of the last item of the previous page, as the old API; no new opaque cursor is exposed. A page
never repeats or skips across stores: both stores order by the same key, and `B` is a total-order position. A deleted boundary
is the evicted-cursor error in both stores. When the legacy table is empty (the ratchet's end state), step 2 skips it and the
ledger is a thin wrapper over `JobService.list`.

### 3.5 Delete

`VideoLedger.delete`: a job id -> `JobService.delete` (owner check, cancel first, tombstone; `JobDeleteConflict` -> 409
`cancel request failed: the job did not stop`, code `video_cancel_failed`, `shapes.py:539`; `JobNotFound` -> 404). Then the
shape calls `prompt_memory.delete_asset("video_<job id>")` and, for the result asset id, `prompt_memory.delete_asset(asset_id)`
(video-91; the Library file removal is not claimed, as the inventory says). Legacy id -> the existing `proxy_graph_video_delete`
(`proxy.py:13519`) unchanged. Body `{id, object: "video", deleted: true}` (video-90, which gets its first direct assertion in V5d).

### 3.6 Orphan marking for job views (frozen)

The reconcile loop (`proxy.py:10649`) marks a record failed when `/queue` and `/history` both lack its prompt id (text at
`:10703`), only from the loop (video-62). For a job the loop is not consulted. The runner owns it:

- `graph_round.run_round` (`engine_protocol/graph_round.py:95`) already polls history and queue; after the prompt was accepted
  (a `prompt_id` was returned), if one poll sees the prompt in neither `/history/<id>` (no entry) nor `/queue`, and a second poll
  `ORPHAN_GRACE_S` (15 s, the loop's cadence) later still does, it raises `RoundFailure(ORPHANED, prompt_id=...)`.
  A poll whose history or queue response is malformed, a wrong shape or unreachable **never** counts toward orphaning (video-63).
- `node_graph_runner.failure_of` (`:121`) maps `ORPHANED` to `RunFailed("upstream_orphaned", "upstream", f"orphaned: {engine} does
  not report this prompt_id", upstream_status=502)`; the text equals the old one.
- The old loop keeps serving legacy records until the table is empty, then goes with it. The loop's ratchet counts it.

This is a named delta for V5d (`orphan_marked_by_runner`, section 5.4): same text, but reached from the runner's poll, not from a
15 s loop over a live table.

### 3.7 V5b files, tests, order

- Owns: `gateway/workflow_runtime_types.py` (the fields above), `gateway/workflow_run.py` (`list`, `request_facts`, progress, facts
  write), `gateway/engine_protocol/graph_round.py`, `gateway/node_graph_runner.py` (`failure_of`, `record_progress`, the two
  `result_processing` stages), `registry/protocols/node-graph.yml` (enum), new `gateway/frontend_shapes/video_views.py`, new
  `gateway/video_ledger.py`, `gateway/media_ingestion.py` (only `index_video_delivery`'s accepted `record`), `proxy.py` hunks:
  `_video_wire_shape` through `videos_delete` (`:27314` to `:27395`) and the reconcile loop's job-view skip.
- Must not touch: `registry/protocol_frontends.yml`, the create/remix/edit/extend handlers (`videos_create`, `videos_remix`,
  `videos_edit`, `videos_extend`), `OpenAIVideoCreate.to_run_request`/`read_wire`, the manifests, `tests/test_s7_parity_video_*.py`.
- Must turn green: `tests/test_video_job_views.py` (extended: facts, progress, content order, delivery headers, 403, evicted
  cursor), `tests/test_workflow_run.py` list/after_key/intents cases (new), `tests/test_video_ledger.py` (new: merged order, cursor
  across stores, tie-break, empty legacy, deleted boundary), `tests/test_node_graph_runner.py` orphan cases (new), plus unchanged:
  `tests/test_video_list_delete_remix.py`, `tests/test_video_job_persistence.py`, `tests/test_video_handler_coverage_gaps.py`,
  `tests/test_video_owner_keys_off_loop.py`, `tests/test_drain_openai_video_jobs.py` (the drain count must include running
  video jobs of the job store; V5b adds that term and keeps the legacy one).
- Order: independent. V5b's tests create jobs with the fake `JobService` (`tests/workflow_runtime_fakes.py`) filled with facts, so
  they do not wait for V5c. V5c's `facts` writer and V5b's `facts` reader meet at the frozen key table in 1.2.

## 4. V5c: the create path

### 4.1 Rows (frozen)

`registry/protocol_frontends.yml` after V5c (`:971` to `:993` today):

- `videos-create` (shape `openai.video.create`, `mode: job`, unchanged routes `POST /v1/videos`, alias `/openai/v1/videos`): adds
  `refusal_bodies: {service_disabled: {message: Video generation not enabled, type: service_unavailable}}`,
  `receipt_opens: before_policy` (the old receipt opened before the policy check for refusals, `proxy.py:11901`; V5d confirms
  against golden `s7_vc_policy_refusal`, which shows policy refusal with 0 differences, so if the golden shows no receipt row for a
  policy refusal V5c keeps `after_policy`; the golden decides), `job_retries: none`, `job_failure_text: adapter`,
  `result_headers: {warnings: X-Proxy-Warning, size_adjusted: X-Azizam-Size-Adjusted, duration_adjusted: X-Duration-Adjusted}`
  (a header applies on the create answer only if the old create sent it; V5d's header table decides each name, none is added
  blind), `result_receipt` as the old receipt fields (4.4).
- `videos-remix`: new, shape `openai.video.remix`, route `POST /v1/videos/{video_id}/remix` (alias `/openai/...`),
  `runtime_policy.intent: video.remix`, `selects: {match: {inputs: {prompt: {type: string}, video: {media_type: "video/*"}},
  outputs: {media_type: "video/*"}}}`, `unconfigured: {status: 501, code: video_remix_not_configured, message: <old text>}`.
- `videos-edit`: gains the same `unconfigured` field (M8) and the same `selects` (it already has it). Both rows are 501 today
  because the shipped manifests declare no `video` input with `media_type: video/*` bound to a required source; an operator turns the
  routes on by installing a manifest that does. `videos.remix_workflow` (`proxy.py:1313`) retires into this. The sentence of the
  old test `test_shipped_config_leaves_remix_off` becomes "no shipped manifest is selected by `videos-remix`/`videos-edit`".
- `FrontendRow.unconfigured` (new field, `workflow_runtime_types.py:579`, parser `frontend_shapes/__init__.py:~60`): V5c adds it;
  M8 said it, no code has it yet. It is V5c's edit to a file V5b also edits (disjoint hunks; the field, not the video fields).
- Extension rows (`videos-extend`) are not created; extend stays on `proxy_graph_video_extend` (section 0).

### 4.2 Validation order and vocabulary

`OpenAIVideoCreate.to_run_request` (V5c overrides the base method at `_base.py:151`, in `shapes.py` outside the lifecycle block).
The order is the old one (`proxy.py:11892` to `:12600`), because several tests pin order (video-29):

1. admission gates (4.3), 2. body read: JSON (`Invalid JSON body`, type `invalid_request_error`, 400) or multipart/urlencoded
(`_read_video_create_form_body`, `proxy.py:11428` to `:11492`; `_VideoFormParseError` -> 400 with `param`, `code: unknown_parameter`
when `param` is set), 3. unknown top-level fields against `STANDARD_VIDEO_CREATE_FIELDS` (`gateway/video_extensions.py:21`;
`code: unknown_parameter`, 400, text from `detect_unknown_top_level_fields`), 4. `video_controls.parse_video_controls`
(`code: invalid_video_control`), 5. delivery control `_note_media_metadata_body`, 6. `prompt must be a string` (param `prompt`),
`prompt is required`, 7. model resolution (`resolve_model_for_operation("videos", ...)`; alias mapping), `model_not_found`,
8. selection (2.4), 9. `seconds`/`size` parsing and bounds (the `seconds must be between ...` text), 10. `input_reference`
resolution (upload to the engine is an adapter staging step, not a shape step: the shape puts the bytes in `media`), 11. subject
select, 12. `extensions.workflow` block.

Every message is produced by the shape through `_wire.error_response(400, text, param=, code=)` with the old text, copied from the
text table V5d publishes in `tests/test_s7_parity_video_create.py` (the eight tests that pin it, plus the table in 5.3). The three
helpers `_gateway_input_error`, `_json_error`, `model_compatibility_error` that the old code calls stay in `proxy.py` until the old
create is deleted; the shape uses `gateway/client_errors.py` / `_wire` equivalents with identical bodies (V5c adds a test that
renders both on the same input and compares bodies byte for byte, then deletes the old copy).

### 4.3 Gates and the answer

| gate | where | wire result | source |
|---|---|---|---|
| video disabled | `RunRefused(503, SERVICE_DISABLED)` from `target_gate` (`workflow_run.py:527`), word from row `refusal_bodies.service_disabled` | 503 `Video generation not enabled`, type `service_unavailable`, 0 engine requests (video-01) | `proxy.py:11925` |
| service not admitting / unhealthy | existing `SERVICE_NOT_ADMITTING`, `SERVICE_UNHEALTHY` | 503 with `Retry-After` as images | `_dispatch_admission_response`, `proxy.py:11918` |
| engine unreachable at submit | new reason `SERVICE_UNREACHABLE = "service_unreachable"` in `workflow_runtime_types.py:62` (V5c, additive), returned by `AdapterAvailability.binding_problem` after the health probe the old route made; `target_gate` turns it into `RunRefused(502, ..., message=port.available.unreachable_message(binding))` | 502 `api_error`, text `<engine label> unavailable at <url>` (video-11), **no job written** | `_graph_video_unavailable_response`, `proxy.py:11085` |
| policy refusal | unchanged (runtime route policy before `to_run_request`) | as today, 0 engine requests (video-04) | |

The 502 text is dynamic (a URL), so it is not a `refusal_bodies` entry; `RunRefused.message` carries it and the row renders it
with the old type. The URL rule from M4 ("failure-text rule") holds: the message names the engine, as the old route did.

Answer envelope (`render_job`, `shapes.py:493`): the same video object as status (3.2) with `status: queued`, `progress: 0`, `model`
(the name the caller sent), `azizam_resolved_model` (`facts.resolved_model`), `size`, `seconds`; `prompt` echo for a caller with no
person id (the old special case, `proxy.py:11840`, kept in `render_job` from `answer_form`); no `Location` header (video-19). HTTP 200.
`x-request-id` is present (video-20): it comes from the row-route owner object, so V5c adds no code; V5d pins it.

### 4.4 Receipts and History

Receipt: opened by the row owner (`row_routes.py:~200`) with `intent` the route's `runtime_policy.intent` (`video.generate` or
`video.remix`) and protocol `openai.video` (row `receipt_protocol`). `status_code` is written on every refusal including the
invalid-JSON 400 (the V3c gap); V5c passes `status_code` from the refusal response in the same place images do. The receipt's
accepted inputs are the wire fields in order (`RunRequest.accepted_inputs`, `workflow_runtime_types.py:412`). Receipt metadata kept
from the result: `result_receipt` names only what the old receipt wrote; V5d's receipt table (video-15) is the list. Differences
that are intended and named in section 5.4: the receipt carries the job id (old `null`), and `manifest_hash` is the contract hash
(`contract_manifest_hash`).

History: the row's workflow name is the receipt name (`receipt_names`); the old entry is `video.generate.<workflow>` with the
`videos.workflows` key (e.g. `video.generate.video-ltx-2.5-t2v`, `proxy.py:11795`). New workflow ids are `video.generate.ltx-2.5-t2v`.
V5c uses an existing `receipt_names` mode only if one reproduces the old string; otherwise V5c does **not** add a mode and V5d lists
the difference as delta `video_history_names_manifest` with exact old and new values. V5c reports which case it is.

Persisting the request record: V5c deletes the call to `_persist_video_record` for new jobs. `facts` replaces it. A failed
`media_jobs.create` is a 500 with no job (the named delta `persistence_failure_visible`, video-16).

### 4.5 Run request assembly

`facts` (1.2) is assembled in the shape after all validation: model, resolved model, size/seconds/actual_seconds/length_frames,
frame_rate, seed (the shape draws the seed when the caller sent none, using the manifest's `generate` rule, so facts hold the value
the engine got), `audio` (only when false), `metadata_mode` (from `media_delivery.current().requested_mode`), `provenance`
(`provenance.build_provenance_record(kind="video", requested_model, resolved_model, workflow_id, backend, seed, parameters={...,
size, seconds, actual_seconds, length_frames}, request_id=<wire id>, created_at)`), lineage ids, `source_asset_id`. The wire id is
unknown before the job exists, so `request_id` in provenance is `video_<run request id>`: the shape stamps a placeholder
`"{wire_id}"` and `request_facts` substitutes the real wire id on read (one string replace, V5b, test pinned). `options` carries
what changes the engine's request or the bytes: `{size, seconds_frames, frame_rate, audio?}` (part of run identity, so a replay with
a different size is a different request). `inputs` carries the manifest inputs (prompt, negative_prompt, seed, width, height,
length, frame_rate, image_strength). `media` carries `input_reference` / `source_video`. `prompt_retention` is the effective
decision, as for images. After `submit_run` returns `disposition == "new"`, the row hook writes prompt memory under
`video_<job id>` (`prompt_memory.safe_put`, `proxy.py:11825`), in V5c's `after_submit` hook of the shape; a failed put changes the
prompt decision to `storage_failed`, as today.

Chain handoff: when step 4 of 2.4 selects a manifest with `video.chain: true`, `to_run_request` returns `LegacyRoute("videos.create")`
(a sentinel class in `frontend_shapes/_base.py`, V5c). `row_routes` answers it by calling the registered legacy handler
(`proxy_graph_video_create`, kept) with the already-parsed body (`body_override`). Count sites in `tests/engine_knowledge_baseline`-style
ratchet `tests/test_legacy_video_ratchet.py` (V5c) that may only fall.

### 4.6 Remix, edit, extend sources

One function, `gateway/video_source.py::resolve_source(wire_id, caller) -> VideoSource | Response`, used by the remix shape, the
edit shape (replacing the raw `asset_id=source` at `shapes.py:586`) and `proxy_graph_video_extend`/`_submit_video_remix` (legacy):

```python
@dataclass(frozen=True)
class VideoSource:
    wire_id: str
    kind: Literal["job", "legacy"]
    asset_id: str               # the stored clip
    model: str | None           # facts.model (the chain extend uses it to pick its engine)
    resolved_model: str | None
    fps: float | None           # facts.frame_rate, or the probe
    size: str | None
    seconds: float | None
    has_audio: bool | None      # probe; None = unknown
```

Rules: invalid id 400 `invalid video_id`; unknown or not the caller's 404 (video-41, video-45, video-54: the job store's `get`
and the legacy `_video_job_access_allowed` give the same answer); a source that is not `succeeded`: the old text of
`_submit_video_remix` for an unfinished source (V5d table). A job source resolves its asset from `view.results` (`ref.asset_id`) and
facts from `request_facts`; a legacy source reads `record["asset_id"]` or fetches through the old `_load_completed_video_bytes`
(`proxy.py:11422`). `probe(asset) -> {fps, width, height, seconds, has_audio}` (the source-video probe; `check_source`'s 24 fps rule
for H3 stays code, `gateway/video_extend_h3.py:248`) lives in this module; it reads the stored file, never the engine.
The edit/remix run request puts `MediaRef(asset_id=source.asset_id)` into `media[<video role input>]`, and `facts.remixed_from_video_id`
/ `extended_from_video_id` = `source.wire_id`. `extend` creates a legacy record whose `extended_from_video_id` is the new-style wire
id; `proxy_graph_video_status` of that record is served by the legacy reader, whose `_tracked_video_fields` is unchanged.

### 4.7 V5c files, tests, order

- Owns: `registry/protocol_frontends.yml` (video rows), `gateway/frontend_shapes/shapes.py` (everything video except the block V5b
  moves), `gateway/frontend_shapes/_base.py` (`LegacyRoute`), `gateway/frontend_shapes/__init__.py` (`unconfigured`, shape
  registry for `openai.video.remix`), `gateway/workflow_runtime_types.py` hunks for `FrontendRow.unconfigured`,
  `SERVICE_UNREACHABLE` and `RefusedMessage`, `gateway/workflow_run.py` hunk in `target_gate` (reason 502), `gateway/row_routes.py`
  (legacy handoff, `unconfigured`, `after_submit` hook), new `gateway/video_source.py`, new `tests/test_video_create_shape.py`,
  `proxy.py` hunks: `videos_create`, `videos_remix`, `videos_edit` and, in the same commit that proves parity, deletion of
  `_proxy_graph_video_create` for the non-chain path and of `_persist_video_record` writers for new jobs.
- Must not touch: the manifests and `workflow.schema.json` (V5a), the lifecycle block, `video_views.py`, `video_ledger.py`,
  `graph_round.py`, `workflow_run.py` outside `target_gate` (V5b), `tests/test_s7_parity_video_*.py` (V5d).
- Must turn green: `tests/test_video_create_shape.py` (new, reads manifests through the in-memory snapshot until V5a merges),
  `tests/test_video_create_sdk_shape.py`, `tests/test_video_limits.py`, `tests/test_video_keyframes.py`, `tests/test_video_subject.py`,
  `tests/test_video_remix_replace.py`, `tests/test_video_extensions.py`, `tests/test_video_extend.py`, `tests/test_video_extend_h3.py`,
  `tests/test_image_video_route_selectors.py`, `tests/test_frontend_row_serving.py`, `tests/test_engine_knowledge_boundary.py`, and,
  with V5d, `tests/test_s7_parity_video_create.py` and `tests/test_s7_parity_video_extra.py`.
- Order: needs V5a's manifest *keys* (frozen, so it reads a dict until merge) and V5b's `RunRequest.facts` field (frozen: V5c writes
  the dataclass field itself in `workflow_runtime_types.py` if V5b has not merged; the two edits are the same three lines and the
  later merge drops the duplicate). Create-only tests run without lifecycle by reading the job through `JobService.get`. A
  complete user journey (create then GET) needs V5b; V5d's end-to-end tests are the join.

## 5. V5d: tests

### 5.1 What changes in the three parity files

`tests/test_s7_parity_video_create.py` (807 lines), `_extra.py` (1713), `_lifecycle.py` (851). Today they assume
`video_<engine prompt id>` (`World.state` strips `video_` and indexes the engine by it, `_lifecycle.py:142`), a
`video_job_records` row (`World.record`, `:157`) and a lifecycle that follows the engine instantly.

Rewrite rules (frozen):

1. **Ids are read from responses.** Never build `video_<pN>`. `create()` returns the id from the body. A test that needs the engine
   prompt id calls `engine_prompt_of(world, video_id)` (5.2).
2. **State through public routes.** Status, content, list, delete through the HTTP rig. The store may be read through the public
   `JobService` only (`world.jobs.get`, `request_facts`), never through SQL on `media_jobs` or `video_job_records`. A test that
   asserted a `video_job_records` row asserts the same field in the status body or in `request_facts` (delta kind
   `record_store_moved`, 5.4); a field with no public surface (the `owner_key_hash`) is asserted by the cross-caller 404, which is
   the behaviour it exists for.
3. **Engine state is driven by the helper, not by flipping `engine.states` and re-reading.** After `drive`, the next status GET
   answers the new state.
4. **Legacy cases** (a record of the old kind, restored from the table) stay: they create the record with the old writer
   fixture `legacy_video_record(world, prompt_id, **fields)` (V5d adds it to `tests/support`; it writes through
   `video_job_records.save`), because the M7 legacy reader is still shipped and still tested. They assert byte-identical old
   output.
5. Text pins are unchanged; do not weaken a status or message.

### 5.2 The rig helper (frozen signature)

In `tests/support/video_rig.py` (V5d owns; used by all three files and by `test_video_*` files that start a job):

```python
EngineState = Literal["queued", "running", "completed", "failed", "missing"]

async def drive(world: World, video_id: str, state: EngineState, *, wait: bool = True, timeout_s: float = 5.0) -> RunJobView:
    """Set the fake engine's state for the job behind ``video_id`` and run the job's runner poll until the job store shows the
    matching state, then return that job's view.

    queued    -> engine reports the prompt in /queue pending; job store: queued or running
    running   -> engine reports it running (history entry not complete); job store: running
    completed -> history_done(...); the runner reads /view and commits the bundle; job store: succeeded
    failed    -> history with execution_error; job store: failed with the adapter text
    missing   -> prompt in neither /history nor /queue; two polls ORPHAN_GRACE_S apart (the test clock advances; no real sleep);
                 job store: failed, text ``orphaned: <engine label> does not report this prompt_id``
    wait=False: set the engine state and trigger one poll without waiting for the state to be reached.
    Raises AssertionError with the last view if ``timeout_s`` (wall-clock) passes without the state."""

def engine_prompt_of(world: World, video_id: str) -> str:
    """The engine prompt id the job submitted: read from the engine's recorded POST /prompt reply, matched to the job by the
    job's order of creation. The one place that knows the mapping."""
```

Semantics: `drive` never mutates the job store. It changes the **engine** (`world.engine.states[pid] = ...`, history mode) and lets
the **runner** run its normal poll step through `JobService.wait(job_id, caller, timeout_s)` with the rig's injected sleep
(`WorkflowCore(sleep=...)`, `workflow_run.py:441`) advancing `world.now` by the poll delay instead of sleeping. So the status that
follows is the one the production runner would write. `wait` is bounded: no sleep loop without `timeout_s`. For the legacy cases
`drive` is not used (the legacy reader reads the engine on each GET, so the old `world.state` flip still works for them; V5d keeps
`World.state` for legacy ids only and asserts the id kind).

### 5.3 Text table (the oracle for V5c and for review)

V5d publishes, first, a table `docs/design/c2d-s7-parity/video-texts.md` (status, body `message`, `type`, `param`, `code`) of every
refusal the old create/remix/edit/status/content/list/delete route can send, taken from running the **old** code on the base commit
(not from reading). It lists at least: `Invalid JSON body`; `prompt must be a string`; `prompt is required`; the `seconds must be
between ...` pair; `size` refusals; `Model '<m>' is not available for video generation.`; `Model <w> is image-to-video and requires
input_reference`; the `long_video_not_supported` text; every `PlanError` text of `video_extend` (`invalid_value`,
`invalid_workflow_extension`, `invalid_shots`); `_VideoAssetError` texts; `invalid video_id`; `video_id not found`; `Video content
is not ready yet`; `No video output found in job`; the 501 `video_remix_not_configured` text; `Video generation not enabled`; the
502 unavailable text. V5c reads the table, V5d tests from it. The table is the first V5d commit, so V5c gets it early; V5c's own
tests do not wait for it (they take their texts from the eight existing pin tests).

### 5.4 Allowed deltas (kinds; each needs a manifest entry with exact paths, old value, new value, reason)

Parent section 4 already allows `idempotency_acceptance` (video-05), `persistence_failure_visible` (video-16),
`template_role_at_load` (video-114), `restart_closes_receipt`, `contract_manifest_hash`. V5 adds exactly these:

| kind | what differs | old -> new | cases |
|---|---|---|---|
| `video_id_namespace` | the id string | `video_<comfy prompt id>` -> `video_mj_<hex>` | every case that prints an id; assertions read ids from responses, so the delta is only "the golden id value", in goldens `video_lifecycle`, `s7_vc_*` |
| `record_store_moved` | the durable store a field is read from | `video_job_records` row / migration row `video_job_records_v1` -> job metadata `request_facts` | video-124, video-125, `World.record` users; the assertion moves to the public surface; the same field values |
| `orphan_marked_by_runner` | who marks the orphan | reconcile loop over `_video_progress` every 15 s -> runner poll with a 15 s grace | video-62 only; text identical, status `failed` |
| `receipt_names_job` | receipt `job_id` and the video workflow name in History | `null` / `video.generate.video-ltx-2.5-t2v` -> job id / `video.generate.ltx-2.5-t2v` (the second only if 4.4 says no existing mode reproduces the old string) | video-14, video-17 |
| `status_follows_runner` | **not a wire delta**: a test-mechanics change | engine flip read live -> `drive` | the tests that flip state between GETs |

Nothing else. In particular none of these may hide a status code, an error body, a header, an engine request (`POST /prompt`
count, `/upload/image`, cancel, `/free`), a receipt row other than the two fields above, or an asset row (`index_video_delivery`
rows, `video-18`). The test-policy gate for the delta manifest (`tests/support/adapter_differential_deltas.py`) rejects a delta
whose paths include `$.http.status`, `$.http.body.error`, `$.http.headers` (other than the named id header values), `$.engine_requests`
or `$.assets`; V5d adds that check if it is not there. The `video-05` `xfail(strict)` at `test_s7_parity_video_create.py:240` and
`_extra.py:376` is removed in the same commit that makes the row dedupe (V5c lands the dedupe; V5d removes the mark in its merge
commit after V5c; until then it stays, strict, so an early XPASS is a loud signal).

### 5.5 V5d files, tests, order

- Owns: `tests/test_s7_parity_video_{create,extra,lifecycle}.py`, new `tests/support/video_rig.py`, new `tests/support/
  legacy_video.py`, `tests/support/adapter_differential_deltas.py` (the five kinds above), `docs/design/c2d-s7-parity/video-texts.md`,
  `docs/design/c2d-s7-parity/video.md` (update the `row path` column), `tests/fixtures/adapter_differential/*.json` only through
  the existing re-record tool for the `video_id_namespace` delta.
- Must not touch: any product code (`proxy.py`, `gateway/**`, `registry/**`, `manifests/**`).
- Must turn green: the three parity files (zero xfail, zero skip beyond today's one), `tests/test_adapter_differential_s7.py` video
  cells, `tests/test_s7_video_chain_graphs.py` (unchanged; chains do not move).
- Order: writes tests first, red on the base, against the frozen interfaces; the rig helper depends on the runner's injectable sleep
  (exists, `workflow_run.py:441`) and on V5b's `ORPHANED` for the `missing` state only. Cases of `missing` are marked `needs V5b`
  until it merges, and run from then on. V5d reports the red count first (parent rule, section 3).

## 6. Order and merge plan

All four start at once. Merge order on the int branch, chosen to keep every merge green: V5a, V5b (disjoint from V5a), V5c (needs
both), V5d last (turns the parity files green; needs all three). Each of V5a to V5c ships with its own unit tests green on its own
branch; the three parity files are expected red until V5c and V5b meet, and V5d reports that red count at each merge.

| shared file | V5a | V5b | V5c | V5d |
|---|---|---|---|---|
| `proxy.py` | - | `:27314` to `:27395`, reconcile skip | `videos_create/remix/edit`, old create deletion | - |
| `gateway/frontend_shapes/shapes.py` | - | one line (base class) | all video except the moved block | - |
| `gateway/workflow_runtime_types.py` | - | `facts`, `request_facts`, `progress`, `list` | `unconfigured`, `SERVICE_UNREACHABLE` | - |
| `gateway/workflow_run.py` | - | `list`, facts write, progress | `target_gate` hunk | - |
| `registry/protocol_frontends.yml` | - | - | video rows | - |
| `manifests/workflows/video-*.yml` | all | - | - | - |

## 7. What this document does not settle (and my confidence)

1. **Chain graphs stay legacy in V5** (section 0). If the design owner wants chain create to move now, M9's composer must be built
   first; I recommend not folding it in.
2. **`receipt_opens` and the History name** (4.1, 4.4) are decided by goldens I did not run (inventory video-14, video-17 say
   "unknown" for the row path). I fixed the rule, not the value. Confidence in the rule: high; in the value: none until V5c runs it.
3. **Exact H3 grid examples** in 2.6 are the formula, not verified numbers; V5a computes and pins them.
4. **`progress` in a runner without engine progress** (a queue-only poll) is 25 in the old code (`_hold_video_progress`,
   `proxy.py:10460`). I froze `record_progress` and monotonicity, not the stage values; V5b copies the old values from
   `_hold_video_progress` and `test_poll_aliases_are_monotonic_capped_and_complete_once`.
5. **Inventory line numbers** in `video.md` (for example `proxy.py:11959`) are from `7daa31769`; this document uses the numbers in
   the base commit (2c7db80fe). They differ by about 90 lines.
6. Nothing here was executed. The base was read, not run; no test was run for this document.

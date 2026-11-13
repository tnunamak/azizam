# T60: store reads and tombstones

`get`, `get_many` and `get_by_request_id` now exclude tombstones in their SELECT by default. `include_deleted=True` explicitly permits retained-row access. Bulk reads retain their existing 500-ID batches; the filter adds no database round trip. Existing read-time queue-position queries are unchanged.

Expect it to work because: `recent` already uses the same NOT IN subquery. The principal risk is a caller treating a hidden job as permission to rerun its request. The audit below preserves claim resolution and legacy replay lookup, and tests exercise both request-ID deduplication and same-key workflow refusal. Confidence before tests: 90%.

## Scope and method

Inspected all production Python calls using AST traversal and `git grep`, including `_media_jobs` in `gateway/api.py`, module-local `get` calls, and imports from `gateway.media_jobs`. No other direct-import aliases of these three reads were found. Locations refer to this change’s source tree. Each call expression has its own row.

58 production call sites: 49 hide; 9 explicitly include deleted jobs.

| Caller | Purpose | Policy | Reason |
| --- | --- | --- | --- |
| `gateway/api.py:267` | Serve the media-job API getter | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/api.py:271` | Serve the media-job API bulk getter | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/api.py:329` | Read the media-job API cancellation response | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/engine_protocol/jobs.py:174` | Check terminal state before protocol recovery | `include_deleted` | Preserve the terminal-state guard: a deleted completed job must not enter protocol submission or recovery. |
| `gateway/engine_protocol/jobs.py:223` | Read adapter or worker job state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/me_make.py:305` | Read adapter or worker job state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/me_make.py:354` | Link dispatch accounting to a job | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/me_make.py:571` | Authorize a source job for a new make request | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/me_make.py:591` | Read worker state, payload recovery or committed state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/me_make.py:597` | Read worker state, payload recovery or committed state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/me_make.py:683` | Authorize an owned make job | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/me_make.py:700` | Build a make response | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/me_media.py:212` | Serve an owned library job | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/me_media.py:2148` | Fold visible jobs into library usage | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/media_ingestion.py:85` | Recover the first indexed response on an idempotency conflict | `include_deleted` | The first response keeps its request ID until reap; retrying the index must keep that record rather than replace it. |
| `gateway/media_jobs.py:1845` | Recover a committed job result | `hide` | Recovery or cancellation must not act on a deleted job. |
| `gateway/media_jobs.py:1855` | Cancel a visible job | `hide` | Recovery or cancellation must not act on a deleted job. |
| `gateway/multipart_adapter.py:269` | Check terminal state before multipart dispatch | `include_deleted` | Preserve the terminal-state guard: hiding the row would become an empty dictionary and permit dispatch of a deleted request. |
| `gateway/node_graph_runner.py:292` | Read adapter or worker job state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/node_graph_runner.py:328` | Read attempt metadata for result commit | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/node_graph_runner.py:436` | Read the adapter cancellation marker | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/node_graph_runner.py:440` | Read adapter metadata | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/node_graph_runner.py:507` | Read output provenance | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/node_graph_runner.py:536` | Check cancellation before storing outputs | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/result_bundle.py:458` | Read the job before publishing its result bundle | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:566` | Wait for upstream submission or terminal state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:599` | Resolve workflow idempotency claims | `include_deleted` | Keep the existing 404 job_deleted response rather than reporting an unreadable claim as temporarily in progress; never schedule the deleted request. |
| `gateway/workflow_run.py:789` | Decide whether a worker may execute | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:804` | Settle an unfinished worker after a crash | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:826` | Close a terminal job’s request receipt | `include_deleted` | Deletion must not prevent accounting from closing the already completed request’s receipt. |
| `gateway/workflow_run.py:869` | Read worker state, payload recovery or committed state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:952` | Read worker state, payload recovery or committed state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:978` | Read retry admission and attempt state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:980` | Read retry admission and attempt state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:1036` | Merge result metadata for a running job | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:1047` | Record progress only for a running job | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:1107` | Settle a worker after its adapter returns | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:1273` | Authorize workflow reads, cancellation and deletion | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:1304` | Read a workflow cancellation result | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `gateway/workflow_run.py:1372` | Read cancellation result before writing the tombstone | `hide` | The read occurs before tombstone_job; a repeated delete already raises JobNotFound through _readable. It needs no retained-row access. |
| `proxy.py:16616` | Job lookup for reports or frontend documents | `hide` | Deleted jobs must not be displayed or used to build a public document. |
| `proxy.py:17295` | Read the newly submitted identity job for its response | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:20657` | Scan legacy video recovery records | `include_deleted` | Retain terminal-state knowledge so a deleted completed job is not treated as unfinished recovery work. |
| `proxy.py:23454` | Decide whether retained prompt payloads belong to active work | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:23755` | Read the cancellation response | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:23852` | Remove terminal jobs’ in-memory or disk payloads | `include_deleted` | A deleted terminal job still needs its retained payload removed; hiding it would skip cleanup. |
| `proxy.py:23864` | Remove terminal jobs’ in-memory or disk payloads | `include_deleted` | A deleted terminal job still needs its retained payload removed; hiding it would skip cleanup. |
| `proxy.py:24464` | Read worker state and cancellation markers | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:24514` | Read worker state and cancellation markers | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:24614` | Read worker state and cancellation markers | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:25231` | Read audio worker state | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:25795` | Read video worker state and cancellation markers | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:25910` | Read video worker state and cancellation markers | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:25967` | Read video worker state and cancellation markers | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:26451` | Deduplicate legacy image submissions | `include_deleted` | Recognize a claimed request ID and refuse a deleted job with 404 before scheduling or constructing a replay response. |
| `proxy.py:26661` | Resolve workflow output rendering metadata | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:26688` | Serve public job content | `hide` | Deleted jobs are absent from this caller’s visible or executable work; no need to read the retained row. |
| `proxy.py:27844` | Job lookup for reports or frontend documents | `hide` | Deleted jobs must not be displayed or used to build a public document. |

## Delete, reap and claims

`WorkflowJobService.delete` reads before writing a tombstone and already refuses a repeated deletion. `tombstone_job` and `reap_tombstones` use direct SQL, so neither needs an include-deleted read. `claim_run_key` and `create` also read directly inside their write transactions; their existing replay-window and request-identity rules are unchanged. Hiding a row through these three APIs does not release its request ID or claim.

The two payload-cleanup lookups and the workflow receipt lookup include deleted rows only for cleanup/accounting. Legacy video recovery includes them to distinguish a terminal deleted job from an unfinished record. Synchronous asset indexing keeps its existing first-response-wins behavior. None of these exceptions authorizes public retrieval.

Tests which inspect physical retention after deletion now explicitly include deleted rows. Normal public and workflow reads keep the default. No callers remain unclassified. In-flight adapter cancellation and publication still depend on their existing terminal-state guards; this change does not introduce transactional synchronization between deletion and an already running adapter.

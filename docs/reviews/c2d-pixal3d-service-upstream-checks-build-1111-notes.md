# Pixal3D upstream checks
Base: b155be8402e2e10d53f19ccf6e2a17d33cfd6006.

Changed only engines/pixal3d_comfy_service.py, tests/test_pixal3d_comfy_service.py and this notes file.
- Added one successful-200/JSON-object validator at queue, upload, submit and history calls. Missing or invalid prompt ids refuse with JSON 502; failed queue reads cannot imply idle.
- A timed-out prompt gets POST /queue with {"delete":[prompt_id]}, then GET /queue. If queue_running contains a tuple/list whose index 1 equals this prompt id, POST /interrupt with {}. This matches the standard ComfyUI route shapes: queue deletion takes a delete-id array, interrupt is a process-wide signal with no prompt-id argument. Cleanup errors are collected into one warning; the deadline still returns 504. Existing finally releases single-flight on every exit.
- GLB response must start with glTF, not merely be nonempty.
- Fake-server cases cover all four JSON call sites with status/parse/object failures, missing prompt id, history becoming non-JSON mid-wait, pending/running cancellation and JSON masquerading as mesh. Existing timeout fake now allows cleanup; happy path asserts no cancellation calls.

Validation executed: Python ast.parse on both changed Python sources, 2 files passed. No imports, pytest cases, integration tests or live ComfyUI requests executed.
Authored case count: 10 existing cases -> 27 cases (+17), by counting parametrization from source, NOT pytest collection or passing tests. Budget file unchanged.
Blocked validation: listed bridge checks do not identify this Python file; requested a targeted unmetered check alias from owner. No unknown broad or paid check was run.
Limitation: /interrupt is global and the standard API has no atomic interrupt-if-id operation; the queue observation guards against knowingly interrupting another prompt, but a queue change between read and interrupt remains possible on a ComfyUI shared with other submitters. This service's single-flight flag is local to its process.
No published catalog, OpenAPI or golden bytes changed. No push/deployment.

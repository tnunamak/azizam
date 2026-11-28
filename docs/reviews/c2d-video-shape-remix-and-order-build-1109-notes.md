# Video remix and create contract fixes

Base: `5e05a4bf8fea6a5bc38a2306e587a396f47b7479`.

## Changes and evidence

- Remix now reads the route's exact `video_id`, adapts it to the edit wire and uses the existing source hook. It uses the edit file policy and bypasses create-only legacy selection, while retaining its create response path: `gateway/frontend_shapes/shapes.py:713-741`. The shared edit resolver selects `video.roles.source` (or a declared video asset fallback), calls the existing hook and records the source asset and wire lineage: `gateway/frontend_shapes/shapes.py:681-710`. The queued response reads lineage from those facts at `gateway/frontend_shapes/shapes.py:633`. Regression assertions: `tests/test_video_shape_contract_order.py:69-82`. The no-workflow control pins 501 `video_remix_not_configured` and forbids source lookup: `tests/test_video_shape_contract_order.py:85-97`. Contract: `docs/design/CODE_TO_DATA_S7_VIDEO_MOVE.md:378-399,478-504`.
- Anonymous create echoes only the validated transient prompt; a person caller and remix do not get that echo. No prompt is added to request facts and the status renderer is unchanged: `gateway/frontend_shapes/shapes.py:626-633`; validation and transient storage remain in `gateway/frontend_shapes/video_create.py:224-231,345-346`. Assertions for anonymous/person create, facts and later status: `tests/test_video_shape_contract_order.py:100-107`. Contract: `docs/design/CODE_TO_DATA_S7_VIDEO_MOVE.md:434-436`.
- Duration-header selection compares `length_frames / frame_rate` directly with requested seconds rather than comparing a rounded fact or applying a tolerance: `gateway/frontend_shapes/shapes.py:635-644`. Frame-grid math is unchanged: `gateway/video_grid.py:88-107`. Tests pin 3 seconds -> 73 frames with a header, exact 73/24 seconds without one, and 3.0417 seconds with one despite matching the rounded fact: `tests/test_video_shape_contract_order.py:110-118`. Contract: `docs/design/CODE_TO_DATA_S7_VIDEO_MOVE.md:167-181`.
- Required secondary asset checks now run after numeric validation and primary-reference staging: `gateway/frontend_shapes/video_create.py:236-247,299-318`. Already staged primary media is excluded from the missing-asset check. Tests pin invalid seconds, invalid size and the unchanged missing-last-image error when those fields are valid: `tests/test_video_shape_contract_order.py:121-132`. Contract order: `docs/design/CODE_TO_DATA_S7_VIDEO_MOVE.md:401-415`.
- The existing duration regression explicitly encoded the contrary tolerance and called 121/24 seconds an exact 5-second duration. Its expectations now match the frozen contract and use 121/24 for the exact fit: `tests/test_video_create_shape.py:240-257`. Its test count is unchanged.

## Test-first sequence and expected base failures

The new test file was applied before implementation. The existing duration test was corrected before the duration implementation. The remix response assertion was added before the lineage response change.

Checks are prohibited for this assignment. The following are source-derived expected failures on the base, not observed test-run results:

1. Remix fails the RunRequest assertion at `tests/test_video_shape_contract_order.py:71`: the base inherits create and does not inject the required video or call the source hook. The hook/media/lineage assertions at lines 72-81 would also fail or be unreachable.
2. The anonymous parameter case fails the prompt assertion at line 103: base create always emits null. The person and status/privacy cases are regression controls.
3. The 3-second and 3.0417-second cases fail the header-presence assertion at line 115 because the base suppresses differences <= 0.05 seconds. The 73/24 case is a true exact-fit control.
4. The invalid-seconds and invalid-size cases fail the message assertion at line 132 because base selection refuses missing `last_image` first. The valid-fields case preserves the existing missing-asset refusal.
5. The corrected existing test fails its small-adjustment header assertion at `tests/test_video_create_shape.py:252` on the base.

## Checks for the lead

No pytest, collection, lint, compilation or repository checks were run. Passing tests are not claimed.

Run the focused files:

```sh
pytest -q tests/test_video_shape_contract_order.py tests/test_video_create_shape.py tests/test_video_create_clock_order.py
```

Then run the related regression files named by the design:

```sh
pytest -q tests/test_video_create_sdk_shape.py tests/test_video_job_views.py tests/test_frontend_row_serving.py tests/test_video_remix_replace.py tests/test_video_keyframes.py tests/test_s7_parity_video_create.py tests/test_s7_parity_video_extra.py
```

New case-count delta: **+10** (remix 1, unconfigured 1, prompt/privacy 2, duration 3, validation order 3). One existing test is corrected without changing its count. The test budget file is untouched.

## Integration and scope

The source-target selection and lineage-return hunks in `OpenAIVideoEdit.to_run_request` are intentionally the same as the concurrent video roles assignment. Apply those shared hunks once when combining its patch with this one. Keep both new test files.

This patch changes only `gateway/frontend_shapes/shapes.py`, `gateway/frontend_shapes/video_create.py`, `tests/test_video_create_shape.py`, the new `tests/test_video_shape_contract_order.py`, and these notes. No root gateway module was opened, no out-of-scope repository files were read, and no publication or deployment was performed.

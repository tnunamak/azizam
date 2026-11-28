# Video shape source roles

Base: `5e05a4bf8fea6a5bc38a2306e587a396f47b7479`.
Assignment: `c2d-video-shape-source-roles-build-1109`.

## Changes and evidence

- Create exempts only the supplied primary reference's declared input, using
  `manifest.roles.get(kind, "input_reference")`, rather than the role word.
  An absent primary or a separate required image is still refused
  (`gateway/frontend_shapes/video_create.py:181-184`).
  The staging path already uses that same role/default mapping (base
  `gateway/frontend_shapes/video_create.py:301-311`).
- Edit selects `video.roles.source`; without it, it selects an asset input accepting
  `video/*` or a concrete `video/` MIME type. It no longer selects the first asset,
  and still refuses a workflow without a suitable source target
  (`gateway/frontend_shapes/shapes.py:676-686`).
- Edit merges its resolved asset reference into existing media and merges
  `remixed_from_video_id=found.wire_id` and `source_asset_id=found.asset_id` into
  existing facts (`gateway/frontend_shapes/shapes.py:703-707`). The source resolver
  and source duration/frame-rate checks remain in place
  (`gateway/frontend_shapes/shapes.py:687-702`).
  This follows the source-role and wire-lineage contract in
  `docs/design/CODE_TO_DATA_S7_VIDEO_MOVE.md:478-505`; the source asset fact is
  specified in that document's request-facts table at line 80.
- Remix is unchanged: its class definition remains untouched
  (`gateway/frontend_shapes/shapes.py:710-714`). Only the two production files,
  the new test file, and this note are included in the patch.

## Tests written first

The test-only patch was applied before any production changes. Fixtures reuse the
in-memory `video_contract`, `snap`, and `read` pattern from
`tests/test_video_create_shape.py:57-105`; no engine or external service is needed.
The test-only diff SHA-256 was
`40dd48daf15e08759df2314ddcb9374322557eb7c5b74f7be23b59d55f29a497`.

No tests or checks were executed, as explicitly required by this assignment.
The following are static expectations for the base, not observed test results:

- The three cases at `tests/test_video_shape_roles.py:41-50` fail at line 47:
  the base returns HTTP 400 instead of a `RunRequest` because it compares the
  required name to `image` (base `video_create.py:181-183`). These cover the named
  `input_reference` role, a renamed `first_frame` role, and the default mapping.
- The absent-primary guard at `tests/test_video_shape_roles.py:53-58` already
  passes on the base. It prevents the exemption from applying without a reference.
- The secondary-image test at `tests/test_video_shape_roles.py:61-66` fails at
  line 66 on the base: its error wrongly names `input_reference` instead of
  `guide` because the supplied primary is rejected first.
- Both explicit-role and MIME-fallback edit cases at
  `tests/test_video_shape_roles.py:100-105` fail at line 104 on the base:
  the media keys are `["guide"]`, not `["video"]`
  (base `shapes.py:677-698`).
- Source-role precedence at `tests/test_video_shape_roles.py:108-116` fails at
  line 115 on the base: media is assigned to `guide`, not `source_clip`.
  An implementation that merely picks the first video would also fail this case.
- The source-facts assertions at `tests/test_video_shape_roles.py:119-123` fail
  on the base because both facts are absent (base `shapes.py:698`).
- Existing-media/facts preservation at `tests/test_video_shape_roles.py:126-149`
  fails first at line 143 on the base. The base also replaces the guide reference
  (line 144 assertion) and omits both lineage facts (lines 146-147 assertions).
  Lines 148-149 additionally protect the original request from mutation.

Case-count change: **+10 expected collected cases across 7 test functions** (the two
parametrizations add 3 and 2 cases). This count is from source inspection, not
pytest collection. The test budget file was not read or changed.

## Lead validation

Run these exact files against the combined final tree:

```sh
python -m pytest -q tests/test_video_shape_roles.py tests/test_video_create_shape.py tests/test_frontend_shapes.py tests/test_video_job_views.py
```

The first three cover these edits and their existing neighbors. The last is the
lifecycle consumer's suite named in the design at
`docs/design/CODE_TO_DATA_S7_VIDEO_MOVE.md:367`. Then run the repository's normal
aggregate checks and account for the +10 cases using its budget workflow.
No passing-test, lint, type-check, or full-suite claim is made here.

## Parallel-patch integration

The parallel `c2d-video-shape-remix-and-order-build-1109` worker was given the
exact edit source-target and merge changes above. Its standalone remix patch may
include those same two edit hunks. Apply identical overlapping edit hunks only
once; retain both assignments' separate tests. This assignment changes no remix
behavior and performs no publication, deployment, metered calls, or Work/Codex task.

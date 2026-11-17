# Video create hand-offs retired

`tests/test_legacy_video_ratchet.py` now pins COUNT at zero. No owner oracle,
shipped manifest, parity golden, model catalog or OpenAPI data was edited.

## Reference-variant facts: original site 174

Accepted in commit 200ccae49. The registry schema requires size, length and grid.
The tests reject each omission from an otherwise valid shipped reference
manifest, then compare a valid variant through POST /v1/videos with the direct
old handler. This removed an unreachable branch rather than a valid request
class.

## Long variants: original site 188

Commit 2f711354b reuses the existing chain validation, segment planner and
composer. The request captures the selected template document and ChainSpec in
its declared run options. The graph runner reconstructs that accepted plan and
composes the submitted graph. It does not reselect a template from live engine
configuration. The old handler keeps its direct submission for the comparison
oracle and extension routes.

Storyboard and 16-second long-variant creates now return job ids. The public
route and direct old handler submit identical engine bodies. Chain availability
still follows the same operator configuration as the old path.

Validation: `456 passed, 4 xfailed in 41.40s`.

## Direct chains and missing facts: original site 163

Commit 0a9f792c7 moves direct long-workflow creates onto the same accepted plan.
Validation: `456 passed, 4 xfailed in 40.85s`.

Commit 7deaaea02 removes the remaining missing-facts hand-off. Registry data in
`registry/video_grid.yml:workflow_fact_source` declares the configuration section,
table, field projection and binding-role destinations. Both paths read the same
current videos.workflows rows, including operator overrides. No owner data moves.

A schema-valid no-section manifest is tested with an operator-set size and frame
rate. Reference variant selection uses the same fact source when the variant
has no video section. The complete submitted graph matches the old handler, including leaving
binding destinations unchanged when the old configuration does not name them.
The ratchet falls to one.

Validation: `457 passed, 4 xfailed in 45.09s`.

## Workflow inputs and media: original site 197

The final change uses the existing input merge and validation functions, image
and video asset resolvers, and source probe. Primary and secondary media are
staged into RunRequest.media and adopted by the job spool. The moved inputs keep
the old upload filenames. Asset bytes and references are excluded from subject
options and the retained workflow extension block.

Source video supplies fps and default duration before frame sizing. Frame counts
use the existing source-cap snap. Subject selection and reference fitting run
through video_subject.apply_point and add_reference_fit in the graph runner.
Edge-only selection inputs stay out of the static graph compiler.

Public/direct-old comparisons cover scalar inputs, named image ids and objects,
URLs, vision-style references, a differently named bound reference input,
secondary keyframe assets, a short source at a different fps, selection points,
and reference fitting enabled and disabled. The graph and upload filenames are
compared. A queued-chain probe also changes the configured chain selection,
removes the live template after acceptance and verifies the captured plan still
submits the old handler's graph.

The video shape inherits the generic no-hand-off hook. Its remaining code has no
LegacyRoute constructor. Existing prompt-id record reads remain covered by the
video lifecycle tests.

No blocker or oracle change remains. Verification uses the hermetic runner with
four workers and targeted files only: the hand-off probes, create shape and role
tests, both boundary ratchets, the four S7 video parity files, adapter differential
S7, video grid defaults, chain composition parity, and the OpenAPI/model metadata
pins. The final command and quoted result are in the delivery report.

# Video create hand-offs: partial result

The missing-facts hand-off after reference-variant selection is redundant.
`registry/workflow_sections/video.schema.json` requires `size`, `length` and
`grid`. `gateway/workflow_contract.py:_structural_errors` applies that schema
before the registry publishes a workflow. A reference variant with any of those
fields absent is rejected. No schema-valid fallback case was found or invented.
The hand-off count falls from four to three; this deletes an unreachable branch,
not a formerly supported request class.

`tests/test_video_handoff_prerequisites.py` checks all three rejected omissions
against a shipped, otherwise valid reference manifest. It also runs the old
handler directly and the public POST /v1/videos route with a valid reference
variant, checks that the route selects the job path, and compares complete engine
submission bodies. The websocket session identifier is pinned for both calls;
the graph and its embedded metadata are not normalized.

## Remaining input hand-off (original site 197)

`gateway/frontend_shapes/video_create.py:_to_run_request` only stages a primary
inline reference. `_reference_bytes` returns None for named assets, URLs and
vision-style objects. Deleting the selection guard would index None at the
reference check instead of resolving the asset. Secondary asset extensions are
currently scalar-coerced into `inputs`, not staged into `RunRequest.media`.

The old path uses `proxy.py:_resolve_video_workflow_assets` to resolve and probe
source video before sizing. For a source shorter than the default duration, the
old path derives seconds and fps from the source; the row path uses manifest fps
and the manifest/default duration. For subject selection, the old path also calls
`video_subject.apply_point` and `add_reference_fit` after binding. The declared
static graph compiler does neither. Removing the whole site needs asset
resolution, source probing and these graph operations in the declared run path.
Scalar-input parity is pinned for a declared steps input; this does not prove
these asset and subject cases and is not counted as a moved site.

## Long-variant hand-off (original site 188)

For a 16-second request to a short workflow with a long variant, the old handler
selects the chain, plans multiple segments, composes their graph and submits it.
`gateway/engine_protocol/node_graph.py:NodeGraphAdapter.compile` instead calls
`compile_workflow` on a static graph. The probe runs the old handler and compiles
the shipped long manifest independently: their graphs differ, and the planned
graph has more nodes. The declared path has no planner/composer invocation.
The existing chain templates and planner can be reused, but must be joined to
that path with pinned template selection and options. Simply selecting the long
workflow would still submit its static graph. Storyboard and long-duration
public submissions are pinned against direct old-handler submissions.

## Initial manifest hand-off (original site 163)

A direct long-workflow create reaches this site even without a variant. It has
the same static-versus-composed graph mismatch as site 188. Its engine submission
is separately pinned against a direct old-handler call. The other case is a
workflow without a video section: the schema permits the section to be absent,
while the old handler can still read sizing and graph roles from the separate
video configuration. `video_grid.video_facts` returns None and the row path has
no typed facts for sizing. Removing this site needs the chain integration plus
a declared source for those missing facts; substituting constants would not
prove parity with the separate old workflow configuration.

No owner oracle, shipped manifest, parity golden, model catalog or OpenAPI data
was edited. No oracle ceiling or reviewed-list change is proposed. The three
reachable sites remain unfinished; their removal has not been claimed.

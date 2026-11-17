# Video create hand-offs retired

`tests/test_legacy_video_ratchet.py` pins COUNT at zero. No owner oracle,
shipped manifest, existing parity golden, model catalog or OpenAPI data was
edited. The retained handler still serves remix and extension routes.

## Main is the reference

Video engine comparisons use the handler bodies copied verbatim from
`proxy.py` at `fd30cbfca`, plus that revision's video-grid module and registry
files. `tests/fixtures/video_fd30cbfca.json` pins those bytes and the source
hashes of the 190 unchanged proxy helpers reachable from the handler path.
The loader binds those helpers to the frozen namespace and the test I/O ports;
it refuses a changed helper body. The branch handler is not the oracle.

The public-route measurements in the `video_*_fd30cbfca.json` fixtures were
recorded on `fd30cbfca` with only the probe test added (proof commit
`3cb3bf253`). `tests/fixtures/video_public_probe_fd30cbfca.py` retains the probe.
The main probe batch passed: `317 passed in 31.77s`.

Graph comparisons use sorted JSON text, so integer and float differences are
visible. Public response tests cover direct chains, long variants and
storyboards. Plain creates with no workflow configuration row remain available
from their manifest; cases needing the separate resolution configuration keep
main's 404. When the manifest has no long variant, shots and over-limit seconds
keep main's 400 answers even if operator configuration names a chain workflow.

## Declared execution

Chain creates reuse the existing planner and composer. Acceptance captures the
selected template and options; execution submits the composed graph. Input
preparation shares the existing merge, extension validation, source probing and
asset resolution steps with the retained handler. Secondary assets enter
`RunRequest.media`. Subject operations call the existing point and reference-fit
functions. Facts with no video section read the declared configuration source.

Number coercion is limited to video creates. The generic binder and frontend
coercion preserve JSON integers; an image public-route test pins an integer at
the engine boundary. Moved video creates retain the stored Library asset kind.

Chain policy tests compare main's 403 body and verify no receipt or engine work
on denial. Success tests compare all 42 non-volatile main receipt fields on both
route spellings. They check the declared job link and the additional generic
admission evidence separately. Chain progress and structured segment failure
text are pinned against the frozen main handler.

## Owner decisions

- Moved chain and keyframe creates may return `video_<job id>` values, including
  `video_mj_*`. Existing prompt-id records must still read.
- A chain whose engine submission fails may answer POST with `200 queued` and
  later expose a failed job. Main answered the error from POST. This change is
  accepted because submission runs through the job path. The test pins the
  failed job's public error as `video_generation_failed` with
  `ComfyUI unavailable` in the isolated fake-engine case. Main's POST error also
  included `at http://comfy.fk.test`; the existing job view removes backend
  addresses. The submit receipt records the queued 200.

## Remaining upload issue

`proxy._submit_video_chain` still uploads an optional first frame before
`submit_run`. A refusal can orphan it, and an Idempotency-Key replay can repeat
it. A move after admission needs a declared media-staging contract for that
optional chain input. This issue remains open.

## Verification

Run the hermetic runner after committing, with four workers and targeted files.
The wider batch includes workflow OpenAPI, video extend, keyframes, extensions,
handler gaps, chain graphs, limits, every `test_video_create_*` file, the main
oracle probes, grid data, image/video selectors and the boundary ratchets. The
final batch also includes the original four S7 video parity files, adapter S7,
video job views, shape order and the frozen route graph. Existing S7 traces and
owner oracles remain untouched. No reviewer was started for this fix round.

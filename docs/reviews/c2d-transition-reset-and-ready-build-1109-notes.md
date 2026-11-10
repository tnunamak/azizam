# Reset settlement and ready-target safety

Base: `a66de5eb5ea2f6fb1b571ecc9f09d7b5dc18e9da`.
Assignment: `c2d-transition-reset-and-ready-build-1109`.

## Result and scope

This patch changes only `gateway/transition_executor.py`, `gateway/transition_snapshot_steps.py`, `tests/test_transition_reset_safety.py`, and this notes file. Reads stayed within the assignment allow-list. The root gateway module and test-budget file were not opened or edited. No checks were run: this assignment explicitly forbids check execution. Existing-test green status is therefore unverified, not claimed.

The regression file was added before either production file changed. The accepted tests-only patch receipt had diff SHA-256 `dffd981b0c2d881a43455f0dfa38a2835bf0cd004833afce10524236ac6db97c`. The subsequent implementation changes use the already-existing settlement and resume mechanisms rather than adding another reset policy.

## Implementation evidence

Line references in this section refer to this patch applied on the stated base.

- `gateway/transition_executor.py:942-952` settles every non-unknown owner restore outcome through `_settle`, passing the stored `operation.step_intent`. The operation resumes first because a failed restore may need to write its reset intent. Continuing outcomes use `_resume_from`; holding outcomes use `_finish_for`. Unknown owner outcomes retain the previous generic hold path at `gateway/transition_executor.py:953-964`.
- `gateway/transition_ops.py:84-95` identifies a reset from a restore intent whose effect key ends in `:reset`. `gateway/transition_snapshot_steps.py:506-514` dispatches that identity to `_after_reset`, rather than using the restore block's no-change attestation.
- `gateway/transition_snapshot_steps.py:484-505` retains the reset-specific attestation, requires declared clean verification for a no-change reset, and distinguishes dirty from unreadable verification. A failed reset cannot borrow the restore attestation.
- `gateway/transition_executor.py:778-786` routes continuation to `_launch_resume`; `gateway/transition_executor.py:628-657` rechecks a resumed wake/select at the open boundary. `gateway/transition_executor.py:788-799` holds an unready target reconciling instead of running OpenStep.
- `gateway/transition_snapshot_steps.py:555-566` requires a nonmissing target, an exactly matching resident offering, and literal `ready is True` when using resident fallback. Freshness/incarnation validation still happens first through `_observed`. Named clean observations retain their existing semantics.
- `gateway/transition_executor.py:324-335` is the pre-existing load-verification rule this resident fallback matches.

## Tests first and expected base failures

These are source-derived expectations, not executed red/green results. All tests below are in `tests/test_transition_reset_safety.py`; they reuse `Engine`, `Reply`, `run`, and `wire` from the existing failure-semantics fakes (import at line 9). The helper verifies that the held intent really is the reset or restore at lines 12-22.

- **Owner reset no-change matrix, lines 25-57 (5 cases):**
  - `restore-attests-only`: the state/gate assertions at lines 48-49 fail on the base, which produces aborted/open despite the dirty, unready target.
  - `reset-attests-dirty`: the base happens to stay failed_closed, but line 51 fails because it never reads the reset clean observation; line 53 would also fail because evidence remains owner-supplied rather than reset verification evidence.
  - `reset-attests-clean`: line 48 fails on the base because it fails closed under the restore block instead of succeeding under the reset attestation and clean check.
  - `clean-but-not-ready` and `clean-unreadable`: line 48 fails on the base because the restore-block decision fails closed instead of applying the required readiness/reconciliation or unreadable-clean hold. Lines 51 and 53 additionally check the actual settlement path and evidence.
  - The reset-count assertion at line 50 prevents accidentally dispatching the already-held reset again.
- **No declared clean verification, lines 60-72 (1 case):** line 68 fails on the base with aborted/open. Line 69 requires reset-specific no-verify evidence.
- **Owner restore no-change, lines 75-93 (2 cases):** line 86 fails on the base for both ready and unready targets because it returns aborted rather than the restore settlement's succeeded or reconciling state. The unready gate assertion at line 87 and snapshot retirement assertion at line 89 independently protect against the unsafe generic path.
- **Owner partial restore, lines 96-108 (1 case):** line 105 fails on the base with failed_closed rather than running the declared reset and continuing. Line 106 requires exactly one reset.
- **Unknown owner restore, lines 111-123 (1 case):** compatibility coverage expected to pass on the base; lines 118-121 preserve the reconciling hold, original intent and no-reset behavior.
- **Successful reset resident verification, lines 126-172 (5 cases):** `not-ready`, `missing-ready`, and `missing-offering` fail state/gate assertions at lines 166-167 on the base, which opens successfully. `wrong-offering` and `exact-ready-target` are compatibility controls expected to pass on the base. The reply changes only after reset at lines 144-154, and line 165 proves verification did reach that post-reset reply. Lines 169-170 require a failed reset settlement to remain at restore with reset verification evidence.

The base causes are `gateway/transition_executor.py:941-956` (generic owner no-change verdict and resume) and `gateway/transition_snapshot_steps.py:555-566` (resident fallback lacking ready and permitting absent offering), using base-file line numbers.

## Lead validation and case budget

Exact focused command for the lead, not executed here:

```sh
python -m pytest -q tests/test_transition_reset_safety.py tests/test_transition_failure_semantics.py tests/test_transition_snapshot_steps.py
```

Then run the repository's required aggregate checks in an authorized environment. In particular, run the existing snapshot-step suite rather than assuming the stricter resident predicate cannot affect its fakes. No test collection, lint, compile, runtime check, or aggregate check has been performed in this assignment.

Case-count change: **+15 pytest cases** in six test functions: 5 + 1 + 2 + 1 + 1 + 5. Existing cases are unchanged. The budget file is untouched.

## Integration with the cancellation assignment

The parallel `c2d-transition-cancel-and-names-build-1109` patch also edits `gateway/transition_executor.py` and `gateway/transition_snapshot_steps.py`. Its resolve cancellation protection moves settlement into an executor-owned, shielded `_resolve_and_hand_off` helper. When integrating, place this patch's entire restore/reset branch at lines 942-952 **inside that helper**, immediately after constructing `result` and `ctx`, not before the shield. Preserve its recovery-pending handling and in-flight resolver protection. Do not restore the generic restore no-change path or compare a normalized reset settlement outcome directly with the owner's input.

The snapshot changes are in separate functions: this patch changes `RestoreStep._clean`, while the cancellation patch changes the effect runner. Re-run both assignments' new tests and the existing suites after integration; neither isolated patch establishes integrated test success.

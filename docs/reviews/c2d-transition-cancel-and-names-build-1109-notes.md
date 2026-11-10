# Cancellation and observation-name patch

Base: `a66de5eb5ea2f6fb1b571ecc9f09d7b5dc18e9da`.
Assignment: `c2d-transition-cancel-and-names-build-1109`.

## Delivery and verification limits

This patch changes only three allowed source files and the two requested output files. No root gateway module, unrelated source, or test budget file was opened or edited. Tests were added before source edits. No tests, lint, type checks, collection, or other repository checks were run, as required by the brief. The base failures below are source-derived predictions, not observed test results. Existing-suite greenness is not verified.

Added: 6 test functions, 8 collected cases (release/load and the two observation fields are each parametrized). Removed: 0. Net case-count change: +8. The budget file is unchanged.

## Fixes and evidence

- Owner resolution now keeps the full outcome settlement, resume, and task handoff in one executor-owned task. `resolve` retains and shields it at `gateway/transition_executor.py:942-951`; the settlement/handoff unit is at `gateway/transition_executor.py:953-990`. The registry at `gateway/transition_executor.py:467` retains strong references, and reconciliation skips these active units at `gateway/transition_executor.py:704`. Failure after a possibly committed write or failed task handoff sets recovery pending and holds the gate at `gateway/transition_executor.py:984-990`. A stale outcome retains its existing exception behavior.
- Core release/load child cancellations become unknown at the child-only `result()` boundary, then use the existing query and settle path: `gateway/transition_executor.py:209-215`. A genuine cancellation while awaiting the executor still propagates, but now marks recovery pending before releasing its lock: `gateway/transition_executor.py:583-593`.
- Snapshot effect child cancellations likewise return unknown at `gateway/transition_snapshot_steps.py:210-213`. Caller cancellation while awaiting the child remains distinct at `gateway/transition_snapshot_steps.py:201-206`; the existing checkpoint query and unknown-hold path remains unchanged at base `gateway/transition_snapshot_steps.py:278-286`.
- Profile loading refuses surrounding whitespace rather than silently accepting a reference the adapter cannot find. Both `servable_check` and `restore.reset.verify` are checked at `gateway/transition_profiles.py:128-140`. The adapter's exact lookup remains unchanged at `gateway/transition_adapter.py:217-222`. No nested caller-owned profile mapping is mutated.

## Test-first base failures

All test locations below refer to the new `tests/test_transition_cancel_holds.py`.

- Lines 37-84: cancel the resolver only after real SQLite resume commits, then release its blocked worker. Line 79's handoff assertion fails on the base: base `gateway/transition_executor.py:956-958` awaits resume in the caller and never reaches launch after cancellation. The final assertions require a succeeded operation and open gate.
- Lines 87-110: inject task-handoff failure after resume. Line 101's recovery-pending assertion fails on the base because no recovery marker is set around base `gateway/transition_executor.py:956-958`. The test restores launch and requires same-process recovery to success.
- Lines 113-145, two cases: an independently cancelled release or load child must be queried and stored as an unknown reconciling operation. The base reaches the explicit failure at line 137 because `CancelledError` escapes base `gateway/transition_executor.py:209-212` and is treated as executor shutdown at base `gateway/transition_executor.py:583-586`. Subsequent assertions check the exact queried step, durable state/outcome, cancellation evidence, and gate.
- Lines 147-158: the snapshot child helper must return unknown with cancellation evidence. The base reaches the explicit failure at line 155 because base `gateway/transition_snapshot_steps.py:210-213` catches only `Exception`.
- Lines 161-199: cancel the actual executor while a release child is blocked. Line 194's recovery-pending assertion fails on the base; shutdown only forces the in-memory gate at base `gateway/transition_executor.py:583-586`. The test drains the detached child, then uses the existing `assert_next_submit_recovers` helper (`tests/test_transition_cancel_recovery.py:46-62`) to verify a durable reconciling hold without restart.
- Lines 202-218, two cases: padded servable and nested reset verification names must be refused at profile load. The `pytest.raises` expectation at line 217 fails on the base: base `gateway/transition_profiles.py:133-138` strips only for membership but preserves the padded reference, while `gateway/transition_adapter.py:219` uses exact lookup.

The asynchronous tests use explicit effect/commit barriers, bounded waits, and cleanup of blocked workers rather than timing guesses about when SQLite commits. They reuse `run`, `wire`, and cancellation/recovery helpers exposed by `tests/test_transition_cancel_recovery.py:12,46-69`.

## Lead validation to run

Run these exact files together first:

```sh
python -m pytest -q tests/test_transition_cancel_holds.py tests/test_transition_cancel_recovery.py tests/test_transition_failure_recovery.py tests/test_transition_failure_semantics.py tests/test_transition_declared_snapshot_ops.py tests/test_transition_admin.py
```

Then run the repository's required full regression/lint/type/budget gates, after applying the separate reset patch if both are landing. Their commands were not inspected because the assignment read allow-list does not include repository check configuration. This report does not claim those checks passed.

## Integration with the parallel reset-safety assignment

Both assignments use isolated workspaces at the same base. The parallel reset patch changes owner restore resolution and `RestoreStep._clean`; this patch changes the same resolve body plus the independent snapshot helper at lines 210-213.

Do not discard either resolve change during integration. Keep this patch's shield, strong-reference registry, and failure fallback. Insert the parallel patch's restore-specific branch inside `_resolve_and_hand_off`, after `result` and `ctx` construction (patched lines 959-961), before `verdict_for` (patched line 962). That branch must resume the row, settle with the stored `operation.step_intent`, and pass continuing results through `_resume_from`; all of those awaits must remain inside this executor-owned unit. The snapshot `_effect` hunk and the parallel `_clean` hunk do not overlap. Run both new regression files after combining them.

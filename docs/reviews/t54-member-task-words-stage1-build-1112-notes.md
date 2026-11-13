# Stage 1 member task words: partial implementation, BLOCKED

Base: 124e8689ba9dd05567fc8f1d9505a0dfb753cc5a.

## Delivered
- Added only the specified member_label/task_name fields, preserving existing values and row order.
- Strict optional-field validation rejects null, non-strings, empty/whitespace-only and padded values.
- New load_capability_member_words returns immutable member-label/task-name projections, using member_label -> label and task_name -> member_label -> label fallback.
- Exports CAPABILITY_MEMBER_LABELS and CAPABILITY_TASK_NAMES.
- Added 24 parametrized Python cases (authored count, not executed): golden/order, invalid values, fallback/legacy shapes, immutability.

## Row-reader audit (gateway/capability_ids.py)
_load_capability_tables is the only capabilities row parser: its unknown-key loop now admits the two fields and validates them. Its second row iteration only uses id to validate hidden_by references, so new keys do not change it. Old positions 0..10 retain their meanings; new maps append at 11..12. _load_capability_data keeps [:4], presentation [4:7], classification [7:10], display order [10], and load_capability_words retains two maps. The import-time tuple is extended to match the private loader. load_modality_words and load_media_type_modalities parse separate sections, not capability rows. Other functions consume unchanged projected maps. No generic consumer outside this allowed module was audited.

## Blockers and omissions
Required new files scripts/generate-capability-words-ts.py and web/src/lib/capability-words.ts are absent from scope_paths/output_paths. They were not created. Generator, generated output, stale-check test and client adapter remain undone; no dangling import was introduced.
Reading web/src/lib/terms.ts hit automatic approval review failure: authorization changed before approval could be applied. This action was not retried or bypassed.
run_check(default) returned capability_not_available: execution=worker while checks require execution=host. Python tests, Vitest and generator were NOT RUN; no runtime/JSON/rendered-byte claim is made.

## Budget
No test-budget or reviewed-domain-literal file edited. Anticipated +24 Python test cases; aggregate count not measured and allowance needs owner reconciliation after full integration. No new web tests in this partial patch.

## Next
Provide scope including both generated-file paths and resolve the approval-review failure. Run focused Python tests and terms Vitest in a permitted runner; finish generator/client integration and staleness coverage before accepting stage 1.

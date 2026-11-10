# Capability presentation order: implementation notes

Base: f8e56732469d179adeb2840dbd0177e137fa68a7.
Assignment: c2d-capability-presentation-order-build-1109.

## Delivery and verification boundary

One patch changes only these six allowed/output files:
- gateway/capability_ids.py
- gateway/owner_projection.py
- gateway/access_preview.py
- registry/capability_words.yml
- tests/test_capability_presentation_from_data.py
- docs/reviews/c2d-capability-presentation-order-build-1109-notes.md

No checks, imports, test collection, test execution, lint, type checks or ratchet
scan were run. This is an implementation with source review, not a passing-test
claim. The large root gateway module and files outside the read allow-list were
not opened. Existing test files and the test budget are unchanged.

## Findings and implementation evidence

All references below are final-patch lines unless explicitly marked base.

1. Reused the existing data home. Capability IDs, labels and intent references
   already live in registry/capability_words.yml, so there is no new registry.
   Added display_order to the existing capability rows, preserving their original
   order, labels and all mappings (registry/capability_words.yml:1-72,73-130).
   The distinction is necessary: the former preview order puts mesh fourth,
   whereas the original row order puts it last (base
   gateway/access_preview.py:53-56 and registry/capability_words.yml:55-58).
   Shipped explicit ranks preserve the preview order exactly.

2. One existing reader validates labels, intent mappings and ranks together
   (gateway/capability_ids.py:19-24,46-76,141-162,197-199). display_order is an
   optional nonnegative integer; bool, negative, fractional, string and null
   values are refused. Ranks must be unique, including collisions with implicit
   zero-based row ranks (gateway/capability_ids.py:71-76). Omitted ranks preserve
   legacy minimal-fixture row order. A MappingProxyType keeps the rank map
   immutable (gateway/capability_ids.py:158-162,186-188).
   Existing four-, three-, three- and two-map public/compatibility loader shapes
   remain intact (gateway/capability_ids.py:165-194). The private aggregate tuple
   gains its eleventh map.

3. Projection retains a closed Literal, now derived from the validated label
   keys (gateway/owner_projection.py:17,27). Receipt and job schemas use this
   type (gateway/owner_projection.py:120,144); the existing intent-map filtering
   still gates projections (gateway/owner_projection.py:267). Unknown intents
   are omitted, and direct unknown capability values are rejected. No free-text
   fallback or new policy decision is introduced.

4. Preview derives GROUP_ORDER from validated ranks
   (gateway/access_preview.py:22,52-53). The offered/permitted union and
   policy comparison are unchanged (gateway/access_preview.py:338-349).
   The sort still uses group rank, group label, aliases first, and lowercased
   name, with undeclared groups last (gateway/access_preview.py:351-356).
   Catalog labels, membership and policy states remain supplied by the catalogs
   and decisions. In particular, a catalog row not declared in presentation data
   must still be shown; that does not make its identity valid for owner
   projection. The test exercises this distinction
   (tests/test_capability_presentation_from_data.py:98-127,140-149).

5. The schema remains closed: unrecognized fields and unknown intent references
   are refused (gateway/capability_ids.py:40-44,59-66,147-151). No production
   import target comes from registry data. An attempted python_import row field
   is specifically refused by the regression guard
   (tests/test_capability_presentation_from_data.py:168-178).

## Test-first record and expected failures on the base

The regression file was applied before any production/data edit. Its first
test-only workspace diff was
bfb85fdc9e75051953d2f82e1957b80c62567aa9a1a0e624d89d578320162bae.
The following are source-derived expectations, not executed red/green results.

- test_unfamiliar_capability_projects_and_orders_from_registry
  (tests/test_capability_presentation_from_data.py:78-137) first fails on base
  during fixture loading at line 86: base capability_ids.py:59 refuses
  display_order as an unknown key. Once rank parsing is present, the assertions
  at lines 91-93 catch the base fixed Literal excluding fixture_weave; lines
  117-119 catch the fixed preview order placing it after known groups.
  Lines 120-127 assert allowed/blocked/unknown decisions and reasons; lines
  130-137 move only the declared rank and require identical entries and counts.
  Permitted-only rows and an undeclared catalog group are included.

- test_display_order_rejects_invalid_ranks has five cases
  (tests/test_capability_presentation_from_data.py:152-157).
  On base, the ValueError message assertion at line 156 fails: the old reader
  reports unknown key rather than the required nonnegative-integer diagnostic.

- test_display_order_rejects_duplicates
  (tests/test_capability_presentation_from_data.py:160-165) similarly fails the
  duplicate diagnostic assertion at line 164 on base.

- test_optional_ranks_preserve_existing_loader_shapes_and_file_order
  (tests/test_capability_presentation_from_data.py:181-194) fails on base at
  line 186 because load_capability_display_order does not exist. It additionally
  checks fallback order, immutability and unchanged compatibility loader shapes.

- test_undeclared_capability_is_rejected_or_omitted
  (tests/test_capability_presentation_from_data.py:140-149) and
  test_capability_rows_still_refuse_unknown_fields_and_references
  (tests/test_capability_presentation_from_data.py:168-178) are preservation
  guards expected to pass on the original base data.

- test_shipped_projection_and_preview_outputs_match_legacy_vocabulary
  (tests/test_capability_presentation_from_data.py:197-228) is also expected to
  pass on base. It fixes the old vocabulary and group order at lines 17-24,
  compares full receipt/job output for every shipped intent at lines 200-209,
  and compares full preview output against the old order at lines 211-228.
  The preview fixture covers every shipped group, allowed/blocked entries,
  alias/model ties and name ordering. This is a compatibility test against
  fixed legacy constants, not an executed baseline snapshot.

Added collected case count: +11 (7 test functions, one with 5 parameters).
No existing test was removed or changed. The earlier progress note saying
12 cases was corrected to 11.

## Literal ratchet and lead integration

Expected reduction: 8 counted sites, matching the assignment's supplied
literal-triage baseline. Both fixed capability rosters were removed:
base gateway/owner_projection.py:27 and gateway/access_preview.py:54-55.
The replacement sites are gateway/owner_projection.py:27 and
gateway/access_preview.py:53 and contain no domain-word literals.
The ratchet matcher counts individual matching strings, not entire tuples
(tests/test_domain_literal_ratchet.py:105-148); the data and test fixtures
are outside its production file scan (tests/test_domain_literal_ratchet.py:151-152).

This eight-site delta is an expectation, not a fresh measured count. The actual
vocabulary and reviewed allowances were outside this assignment's read scope,
and running the scan was prohibited. The lead must confirm the measured delta.
No production domain-word literals were added by this patch.

The test budget was deliberately not edited. The ratchet requires exact equality
as well as an upper bound (tests/test_domain_literal_ratchet.py:340-351).
Consequently, after the expected reduction the existing ratchet equality test
will need the lead's matching budget adjustment; this patch alone cannot be
claimed to keep that stale-budget assertion green. Do not raise the budget.

## Exact test files for the lead

Run the focused compatibility and new regression suite:
python -m pytest -q tests/test_capability_presentation_from_data.py tests/test_access_preview.py tests/test_owner_projection.py

Measure the literal delta, inspect reviewed/unreviewed sites, and lower the
existing budget accordingly:
python tests/test_domain_literal_ratchet.py

Then run:
python -m pytest -q tests/test_domain_literal_ratchet.py

Finally run the repository's normal required suite/gate for the final integrated
tree, including other capability reader consumers. No check receipts are
attached because this assignment explicitly forbids executing checks.

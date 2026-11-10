# STT sample recipe source roles

Base: `f8e56732469d179adeb2840dbd0177e137fa68a7`.
Assignment: `c2d-stt-recipe-source-role-build-1109`.

## Delivered change

- Added version-1 descriptors to the existing registry, not a new file:
  `registry/stt_samples.yml:22-39`. Each descriptor supplies its algorithm ID,
  required source parameters, other required parameters and conversion source role.
- The only executable choices are the fixed callable allowlist in
  `gateway/stt_samples.py:266-271`. Descriptor validation rejects unknown IDs
  (`gateway/stt_samples.py:373-375`), and reference generation independently refuses
  an unknown algorithm (`gateway/stt_samples.py:275-281`). No dynamic import or
  Python path lookup is introduced.
- Descriptor validation is closed at both levels and rejects unsupported versions,
  malformed parameter lists, duplicates, overlapping roles and a conversion role
  absent from the required source parameters
  (`gateway/stt_samples.py:357-397`).
- The frozen descriptor is carried by each sample
  (`gateway/stt_samples.py:92-112,486-489`). Both the rights check
  (`gateway/stt_samples.py:474-476`) and conversion
  (`gateway/stt_samples.py:634-636`) use its `conversion_source_param`.
  There is no replacement hard-coded recording key.
- Sample-only manifests and older directly constructed SampleSpec values use the
  same bundled data (`gateway/stt_samples.py:400-413,503-506`).
  Explicit descriptors remain local to their loaded samples.
- Parsing, interpolation, normalization, sorting and cut behavior are retained:
  algorithm bodies at `gateway/stt_samples.py:153-263` are unchanged;
  dispatch/cut/sort are at `gateway/stt_samples.py:275-285`.
  Existing sample entries, download and ffmpeg implementations are unchanged.

## Test-first evidence and expected base failures

The complete new test functions were applied before the production patch.
No tests, syntax checks, scans, linters or repository commands were run because
this assignment explicitly forbids checks. The following are source-based
expectations, not recorded red/green executions.

- `test_recipe_source_role_is_declared_once`, three cases
  (`tests/test_stt_recipe_source_role.py:43-94`): uses an unfamiliar recipe kind
  and `payload_blob` role with an `audio`-named decoy. The selected source's
  not-applicable rights must be refused (lines 61-63); with stated rights, the
  conversion must receive that same source and the original reference words
  must survive with the existing cut behavior (lines 90-94).
  On the base, the rights-message expectation fails because `recipes` is an
  unknown top-level key; the successful loading/conversion assertions are also
  unsupported. Base evidence: `gateway/stt_samples.py:420-428`.
- `test_recipe_descriptors_refuse_invalid_data_before_execution`, 21 cases
  (`tests/test_stt_recipe_source_role.py:97-131`): version, structure, unknown
  fields/algorithms, import-path-like IDs, missing/unknown roles and missing
  sources are refused with specific errors. Every allowed callable is replaced
  by a spy, and execution must remain empty (lines 125-131).
  On the base, all 21 expected diagnostic matches fail: its generic unknown
  top-level `recipes` error does not contain any of the required diagnostics.
- `test_samples_only_manifest_uses_bundled_descriptors`
  (`tests/test_stt_recipe_source_role.py:134-140`): existing samples-only fixture
  loads with the shipped algorithm and role. Base loading works, but the
  descriptor assertions fail because SampleSpec lacks that attribute
  (base `gateway/stt_samples.py:92-103`).
- `test_unknown_algorithm_cannot_execute_after_spec_construction`
  (`tests/test_stt_recipe_source_role.py:143-148`): even a manually replaced
  descriptor cannot dispatch `os.system`. Base fails during fixture loading
  before this assertion because it cannot load descriptors.
- `test_loaded_descriptor_does_not_change_with_a_later_manifest`
  (`tests/test_stt_recipe_source_role.py:151-162`): separately loaded manifests
  cannot overwrite each other's role. Base fails during initial loading.
- `test_legacy_spec_without_descriptor_uses_bundled_role`
  (`tests/test_stt_recipe_source_role.py:165-183`): legacy SampleSpec fallback
  preserves conversion selection and reference words. Base cannot construct
  the replacement with the new optional field, so the success assertions
  cannot be reached.

All new conversion and download entry points are stubbed, with default refusal
guards at `tests/test_stt_recipe_source_role.py:15-22`, explicit local stubs at
lines 74-89 and 176-180. Reference algorithms read only miniature local fixtures
from `tests/test_stt_samples.py:331-383`; no upstream service is contacted.

## Counts and integration follow-up

- Added **28 pytest cases**: 3 role cases + 21 refusal cases + 4 standalone
  compatibility/dispatch/isolation cases. Existing test files were not modified.
- Expected literal-site decrease: **5**. The base has three `"audio"` source
  role literals at `gateway/stt_samples.py:259-261`, plus selection literals at
  lines 402 and 556. All five move to the registry; replacements use descriptor
  fields. The ratchet matches whole family literals
  (`tests/test_domain_literal_ratchet.py:106-114`) and scans gateway code, not
  registry YAML (`tests/test_domain_literal_ratchet.py:153-154`).
  This is a source-based delta, not an executed full scan.
- **The lead must reconcile the budget outside this patch.** The budget was not
  read or edited. The existing ratchet requires exact equality at
  `tests/test_domain_literal_ratchet.py:350`, so a confirmed five-site reduction
  requires lowering `[ratchets] domain_literals` by five in the integration
  change. An unchanged budget would deliberately fail that existing test.
  Account for any other simultaneously integrated patches rather than blindly
  applying this delta twice.
- Nothing was pushed, published or deployed. The large root gateway module was
  not opened. All repository reads stayed within the four allowed input paths.

## Exact lead-run checks

Run with the repository's configured Python environment:

1. `python -m pytest tests/test_stt_recipe_source_role.py tests/test_stt_samples.py`
   (the new file is entirely stubbed/offline; the existing file uses local fake
   HTTP and its existing ffmpeg availability marker at
   `tests/test_stt_samples.py:25-26`).
2. `python tests/test_domain_literal_ratchet.py` to verify the five-site delta
   and reconcile the budget as described above.
3. `python -m pytest tests/test_domain_literal_ratchet.py` after that adjustment.
4. Run the repository's normal aggregate checks, which this assignment cannot
   inspect or execute.

Test files: `tests/test_stt_recipe_source_role.py`,
`tests/test_stt_samples.py`, `tests/test_domain_literal_ratchet.py`.
No executed pass or full-suite compatibility claim is made.

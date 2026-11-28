# Presentation vocabulary duplicate rejection

Base: `681fd96f32dea2b8bc142d3f59a7347830422994`.
Assignment: `c2d-presentation-duplicate-words-build-1109`.

## Change and evidence

- The base loader checks list type, emptiness and strings but not repeats (base `gateway/model_presentation.py:67-71`). The seven affected keys are declared at `gateway/model_presentation.py:23-26`; the shipped lists are at `registry/presentation_words.yml:3-7,16-27`.
- The fix retains the existing validated tuple, then tracks exact strings and raises on the first repeat (`gateway/model_presentation.py:71-76`). It neither sorts nor deduplicates, trims or folds case. The existing error helper includes the input path, and the new error includes the key, zero-based index and `duplicate word` (`gateway/model_presentation.py:35-36,75`).
- The regression appends the first item to each of the seven lists and checks the path, exact indexed key and diagnostic (`tests/test_presentation_words_rows.py:209-212,226-237`).
- The shipped provider entries deliberately differ by case (`registry/presentation_words.yml:3-5`); exact-string comparison preserves that distinction (`gateway/model_presentation.py:72-76`). Existing case and cleanup expectations remain unchanged (`tests/test_presentation_words_rows.py:59-90`).
- The shipped YAML is unchanged. Import loads it through the same validator (`gateway/model_presentation.py:22,98`). Existing tests compare the actual YAML with the literal fixture and check ordered loading, including both provider spellings and compiled patterns (`tests/test_presentation_words_rows.py:145-164`). These provide the positive control for the lead's run; no successful load is claimed from execution here.

## Test-first record and expected red cases

The test-only patch was applied before the five-line implementation change. Its bridge diff SHA-256 was `fd5824349d4a00a912f6d07b6dff8daddbd2b4462a7905c83f69988703f4494e`.

Each new case in `test_every_list_refuses_duplicate_words_with_file_key_and_index` is expected to fail on the base at `with pytest.raises(ValueError)` (`tests/test_presentation_words_rows.py:232-233`), with "DID NOT RAISE". The later diagnostic assertions would not be reached. All repeated entries remain non-empty strings; the repeated technical pattern is already valid (base `gateway/model_presentation.py:38-41,67-71,83-89`; `registry/presentation_words.yml:3-7,16-27`).

The seven case IDs and expected duplicate positions are:
- `provider_suffixes`: index 2.
- `engine_prefixes`: index 2.
- `engine_tails`: index 3.
- `parenthesized_hardware`: index 2.
- `hardware_label_suffixes`: index 4.
- `bare_titles`: index 2.
- `technical_patterns`: index 8.

Indices come from the shipped lists above and the append construction at `tests/test_presentation_words_rows.py:228-230`. This is static red analysis, not an observed failing run.

## Verification handoff and counts

The assignment explicitly says this job cannot run checks. No test, collection, lint or other repository check was executed. Pass/fail counts are therefore unavailable; existing tests being green remains to be verified by the lead.

Static case inventory, expanding the visible parametrizations in `tests/test_presentation_words_rows.py:49-314` and `tests/test_model_presentation.py:27-778`:
- Presentation words: 129 existing + 7 new = 136 cases.
- Model presentation: 109 existing, unchanged.
- Combined: 238 existing + 7 new = 245 expected cases.
- Case-count change: **+7**. No test-budget file was read or edited.

The lead should run these exact files from the repository's prepared test environment:
```sh
python -m pytest -q tests/test_presentation_words_rows.py tests/test_model_presentation.py
```

To observe the red phase, apply only the test hunk to the stated base and run:
```sh
python -m pytest -q tests/test_presentation_words_rows.py -k test_every_list_refuses_duplicate_words_with_file_key_and_index
```

Expected red selection: 7 failing cases, 129 deselected, provided normal collection succeeds. Then apply the implementation and rerun both files. Counts above are source-derived expectations, not pytest receipts.

Only `gateway/model_presentation.py`, `tests/test_presentation_words_rows.py`, and this notes file are changed. The root gateway module and all out-of-scope repository files were not opened. Nothing was published or deployed.

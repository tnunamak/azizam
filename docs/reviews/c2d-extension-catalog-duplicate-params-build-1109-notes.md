# Duplicate extension path parameters

Base: `7c548bb044d078a86b947ed6354281ae78c99ecb`.
Assignment: `c2d-extension-catalog-duplicate-params-build-1109`.

## Test first and expected red behavior

The test-only patch was applied before the loader change (test-only diff SHA-256:
`f8bba2d952db5ee1773f6f2e10400f0f05b53ddc6db29a0ac20d9c039efa79ef`).
The three new cases are `implicit-converters`, `same-converters`, and
`different-converters` at
`tests/test_extension_catalog_converters.py:92-97`.

Each case first asserts the router refuses the canonical path with
`Duplicated param name` (`tests/test_extension_catalog_converters.py:98-99`).
The loader assertion requires a `ValueError` at
`tests/test_extension_catalog_converters.py:101-102`, then compares the whole
message, including file, record 1, row 2, parameter 'id', and the path
(`tests/test_extension_catalog_converters.py:103-105`).

On the base, all three new cases are expected to fail at the loader
`pytest.raises(ValueError)` context with DID NOT RAISE. The exact-message
assertion is consequently not reached. This is a source-based prediction,
not an executed red run: the base parameter loop checks only converters
(`gateway/extension_catalog.py:113-116` at the base SHA), and str, int, and
uuid are accepted converters. The fixture makes the bad route row 2 after
a valid route (`tests/test_extension_catalog_converters.py:100`), avoiding
an earlier row/order collision. The existing helper assigns distinct orders
(`tests/test_extension_catalog_converters.py:18-24`).

## Minimal fix

A fresh name set is created for each route; the existing parameter loop rejects
a repeated name before exposing the route
(`gateway/extension_catalog.py:113-121` in the patch).
The error is `{file}: record {record}, row {row}: duplicate path parameter {name!r}: {route!r}`,
using the existing location and unknown-converter message style
(`gateway/extension_catalog.py:110-120`).
Converter validation still happens before the duplicate-name check
(`gateway/extension_catalog.py:115-120`), and PARAM_REGEX is unchanged
(`gateway/extension_catalog.py:114`).

No other loader behavior or existing test body was changed.
Existing converter/literal coverage remains at
`tests/test_extension_catalog_converters.py:56-89` and `:108-138`.
The existing shipped-count assertion remains at
`tests/test_extension_catalog_converters.py:158-166`:
prompts 9, workflow 4, chat 2, routing 2.
The unchanged literal catalog and golden-summary checks are at
`tests/test_extension_catalog_rows.py:52-60` and `:216-222`.
Their green status remains unverified in this assignment.

## Verification handoff and case counts

The brief says this job cannot run checks. No test command, collection, or
repository check was executed. The later request to run both files is therefore
handed to the lead. Run exactly:

```sh
python -m pytest -q tests/test_extension_catalog_converters.py tests/test_extension_catalog_rows.py
```

To verify red behavior, apply only the test-file hunk to the base and run:

```sh
python -m pytest -q tests/test_extension_catalog_converters.py -k duplicate_parameter_name
```

Static case counts, not collection or pass receipts:
- `tests/test_extension_catalog_converters.py`: 7 -> 10 cases, +3.
  Its existing two-case parameterization is at lines 56-66, the new three-case
  parameterization is at lines 92-97, and five other tests remain single-case.
- `tests/test_extension_catalog_rows.py`: 27 -> 27 cases, unchanged.
  This is 17 test functions plus parameterization expansions at lines 63-67,
  73-89, 95-114, 125-137, and 257-258 (2, 5, 3, 2, and 3 cases).
- Combined: 34 -> 37 cases. Repository case-count delta: +3.
  The test budget file was neither opened nor edited.

The patch changes only the loader, converter test file, and this notes file.
The root gateway module was not opened. No publication or deployment occurred.

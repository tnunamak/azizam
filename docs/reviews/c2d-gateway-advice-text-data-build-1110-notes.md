# Gateway advice text as data

Base: `638af7b7f7790fc958d7bb68dd9c0643d83d2e74`.
Assignment: `c2d-gateway-advice-text-data-build-1110`.

## Delivered scope and design

- The patch changes only `gateway/extension_catalog.py`, `gateway/toolcall_detection.py`, and the three authorized output files. Existing test files and the test budget are unchanged. The large root gateway module was not opened.
- At this base, the two extracted literals really are `gateway/extension_catalog.py:185-187` and `gateway/toolcall_detection.py:109-112`; these are base line numbers, not post-patch locations.
- Shared release copy now lives at `registry/gateway_messages.yml:1-12`. The metadata description remains complete, including its engine name; the remediation still names the protocol and retains the family, optional backend label, and unknown-family fallback. Folded YAML scalars use `>-` to preserve spaces without adding a trailing newline.
- The scoped existing registry reader owns registered route facts, not shared prose (`gateway/extension_catalog.py:67-156`). The service-specific advisory is explicitly operator-authored and optional (`gateway/toolcall_detection.py:135-160`). Neither is an appropriate owner for both kinds of shared copy, so this uses the new message file requested by the brief. No unrelated registry files were inspected.
- The shared loader is deliberately in an already-authorized module rather than a new Python module. `gateway/extension_catalog.py:15` imports that module and `gateway/extension_catalog.py:185` reads its immutable metadata text. `gateway/toolcall_detection.py:91` loads release copy once at import. Data edits take effect on restart/reload, not via per-request filesystem reads.
- The flat schema requires exactly version 1 and the four text keys. Unknown/missing keys, nonmapping documents, Boolean/float/string versions, and blank/nonstring text fail closed (`gateway/toolcall_detection.py:45-87`). The result is a read-only mapping.
- Template placeholders must be the exact allowed field names, each once. Attribute/index access, conversions, nonempty format specifications, missing/repeated/unknown fields, and malformed braces are refused (`gateway/toolcall_detection.py:51-65,85-86`). No schema key selects a Python import, callable, backend, or dispatch path; YAML is read with `safe_load` (`gateway/toolcall_detection.py:68-87`).
- The advisory has no backend-name comparison or dispatch: `backend_hint` is only interpolated if nonempty, the family fallback remains explicit, and the operator suffix has its existing truthiness and stripping behavior (`gateway/toolcall_detection.py:153-160`). Marker detection, stream processing, normalization, route rows, and control names/defaults are not changed. The old misleading substring-matching comment is corrected at `gateway/toolcall_detection.py:130-133`.

## Tests written before implementation

The new test file was applied first, before either production-module change or the registry file. No checks, imports, pytest collection, or test execution were run, as the assignment prohibits checks. Base outcomes below are static expectations, not observed test receipts.

- `tests/test_gateway_messages_data.py:58-73` freezes the shipped catalog against the existing all-audience golden and the literal metadata descriptor (`:15-24`), then compares UTF-8 advisory bytes with a separately frozen base string (`:30-40`). It covers every marker family, empty/unfamiliar families, known/unfamiliar/empty backend labels, literal braces, and absent/empty/whitespace/padded operator overrides. This compatibility test is expected to pass on the base.
- `tests/test_gateway_messages_data.py:76-109` is the requested `test_catalog_and_advisory_copy_are_data`. It changes only the four text values in a temporary copy of the registry, reloads the validated mapping, and expects changed catalog/advisory output. It also requires unchanged non-copy catalog facts, identical route registry identity, identical markers, unchanged detection gates, and unmutated payloads. The operator override must still appear, and arbitrary backend labels use the same template.
- On the base, the proof test stops at `:81` because the message file is absent. The schema-closure test (`:112`) likewise lacks the file, and the remaining validator tests (`:124,133,140,170`) lack `load_gateway_messages`. With only the data fixture supplied, the loader/state lookup at `:88` and all validator calls still fail. These are missing-interface/data failures, not claimed observed assertion failures.
- The behavior assertions newly required beyond the base are `:92,107-109` (replacement copy reaches both consumers), `:115-121` (unknown/missing keys refused), `:126-130` (shape/version refused), `:136-137` (invalid text refused), `:160-163` (unsafe templates refused), and `:172-176` (closed immutable loaded keys and missing-file failure). Attribute/import-path configuration is specifically covered at `:114-116,146-156`.
- Literal braces are permitted as plain text via doubled braces and tested at `tests/test_gateway_messages_data.py:164-167`; inserted family/backend labels are not recursively formatted.
- Existing compatibility coverage to retain includes `tests/test_extension_catalog_rows.py:217-223` (all-audience golden) and `tests/test_toolcall_advisory.py:226-279` (generic engine-agnostic advice, operator suffix, and ASCII header handling). No existing golden was recaptured or modified.

## Exact validation handoff

Run these exact test files after integrating the patch and adjusting the ratchet budget:

```sh
python -m pytest -q tests/test_gateway_messages_data.py tests/test_toolcall_advisory.py tests/test_extension_catalog_rows.py tests/test_domain_literal_ratchet.py
```

Then run the repository's required aggregate checks. Also verify release packaging includes `registry/gateway_messages.yml`, because it is required at module import (`gateway/toolcall_detection.py:45,70,91`). No packaging files were in this assignment's read scope.

Status: all checks are **not run**, not passed. The bridge accepted the test-first patch and implementation patch; source/diff review is not a substitute for execution.

## Counts and integration prerequisite

- **Pytest case delta: +7.** Seven non-parametrized new test functions at `tests/test_gateway_messages_data.py:58,76,112,124,133,140,170`. Loops add coverage without adding collected cases. Existing cases are unchanged.
- **Expected literal-site delta: -2.** One former string literal containing the engine name at base `gateway/extension_catalog.py:185`, and one adjacent-string constant containing the protocol name at base `gateway/toolcall_detection.py:109`, move outside `gateway/` into the YAML file. The replacement lookups introduce no product-name decision. This is a static expected delta; the full vocabulary scan was not run.
- The scanner counts string constants/chains/identifiers at `tests/test_domain_literal_ratchet.py:116-149` and scans the root module plus `gateway/**/*.py` at `:152-153`; the new YAML and new test file are outside that scan.
- **The lead must lower `[ratchets] domain_literals` by 2 for this patch's contribution**, reconciling any other integrated patches. This job did not read or edit `tests/budgets.toml`. The existing ratchet requires exact equality at `tests/test_domain_literal_ratchet.py:341-351`, so this scoped patch alone is expected to fail that equality until the lead applies the authorized budget update. Do not report all existing tests green before that integration step and actual execution.

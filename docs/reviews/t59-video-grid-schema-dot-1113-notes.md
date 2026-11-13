# Train 59 video-grid schema patch
Base: tnunamak/azizam 249fd4d8e734b2f68c079865ed5de8518c0d8e6b.

Expect it to work because the existing status_words loader uses Draft202012Validator, and this patch validates a separate string-key copy while returning the original integer-key lookup shape. Confidence: 85%, unverified.

Changes: closed schema at every object level; wire-token strings (including rejection of trailing newlines); required default/by_step; Python exact-int and positive-key check before string normalization; import-time path-named ValueError; unchanged stride_reason implementation and monkeypatchable _STRIDE_REASONS. No schema_version added, budget edit, YAML data change, publication or deployment.

Added 15 pytest cases in the existing tests/test_video_manifest_data.py: 1 valid shipped-file/lookup case, 9 parametrized invalid-reason/document cases and 5 invalid-step cases. No new test file.

Tests deliberately NOT RUN, as instructed. No available Dalan checks were offered. This is complete code, not a verified runtime result.

Assertions least certain until owner runs tests:
1. yaml.safe_dump/safe_load preserves the distinction among quoted string "8", bool true and float 8.0 in the isolated invalid-key cases.
2. jsonschema's additionalProperties/patternProperties behavior for the normalized dictionary matches Draft 2020-12; type(step) is int guards bool before normalization.
3. Existing test module's broader imports/fixtures still collect in the owner environment.
4. Existing test_status_words monkeypatch remains compatible; the attribute shape is intentionally unchanged.

Suggested owner validation: run tests/test_video_manifest_data.py and the existing test_video_grid_reasons_keep_the_published_values_and_read_data in tests/test_status_words.py. Confirm no unrelated budget/schema snapshots need maintenance. No test-count budget adjustment was made.

# Catalog unclassified words, round 3

Base: 583c4d1af4a2a7db27872754714287ebf3a5c943.

Added a dynamic unclassified_capability_words accessor in model_presentation; catalog fallback label/icon read that row. Each loop iteration classifies its capability once.
Added two test cases in the existing catalog test file: current row values; monkeypatched words plus one classification call. The first also asserts label/icon key-set parity and exposes differences on failure.

Tests executed: none (0 executed; pass/fail counts unavailable). Catalog, presentation and word-pin suites remain unrun because no registered check identifies that suite set. Requested an exact check route through bridge progress; no answer received before this note.
Test-case delta: +2 non-parametrized test functions; collected suite counts unverified. Budget unchanged.
Label-only/icon-only keys: unverified until runtime check; their defining imported source is outside the read allowlist. No capability enumeration attempted.

Presentation YAML unchanged. Current fallback literals equal the row values, supporting source-level equivalence; published catalog/OpenAPI/golden byte equality has not been executed. No published artifacts, budget or data files changed. Patch needs requested checks before acceptance.

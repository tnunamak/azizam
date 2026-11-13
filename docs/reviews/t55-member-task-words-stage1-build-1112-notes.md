# Member task words, stage 1 continuation

## Outcome: partial, blocked at client adapter
Base: 520ac3ab9b2c4d8a571a5221545b9631db628a97.

Kept the prior data and loader changes unchanged. Added a pure generator using the named validated loader, its TypeScript projection, a Python --check staleness test, and a full twelve-string taskNames Vitest assertion.

web/src/lib/terms.ts was neither read nor edited: the earlier authorization denial remains a blocker, and was not retried or bypassed. Change 4 therefore remains undone. No other client source file changed, and the t54 notes remain untouched.

## Data and loader inspection
All twelve effective task strings and two member-label overrides match the brief in the pinned data. Both optional keys reject null, non-string, empty, whitespace-only and padded values. MappingProxyType wraps both new maps.
_load_capability_tables is the only capability-row reader in gateway/capability_ids.py. Its whitelist includes the new keys; it validates them and appends the new maps at positions 11 and 12.
Existing readers preserve their projections: _load_capability_data [:4], load_capability_presentation [4:7], load_capability_classification [7:10], load_capability_display_order [10], load_capability_words via the four-map reader. The module-level unpack includes the new two maps. load_modality_words and load_media_type_modalities inspect other top-level sections rather than capability row keys. No other reader was found in this file.

## Validation and owner follow-up
No Python, Vitest or host checks ran. This assignment has no available_checks. The committed TS text was constructed to match the generator's deterministic rendering from the inspected twelve rows; the Python generator itself was not executed. Owner must run it and --check before accepting the patch.
Expected commands:
- python scripts/generate-capability-words-ts.py
- python scripts/generate-capability-words-ts.py --check
- pytest tests/test_capability_words_data.py tests/test_product_contracts.py
- the existing Vitest invocation for web/src/lib/terms.spec.ts
The Python file gains one collected test and the Vitest file gains one test. The prior 24 member-word cases remain unchanged. Do not treat these as passed tests. Test-budget and reviewed-domain-literal files were not read or modified; reconcile allowances with the added tests and generated capability-word literals if their checks require it.

## Remaining adapter change
After authorized access is available, preserve taskNames's twelve keys/order and map them to capabilityTaskNames: sound_effects -> sfx, decisions -> decide, embeddings -> embed, reranking -> rerank; all other keys map directly. Do not modify unrelated terms.

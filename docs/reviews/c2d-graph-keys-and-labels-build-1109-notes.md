# Partial build: graph keys and diagnostic labels

Request: c2d-graph-keys-and-labels-build-1109
Base: f8e56732469d179adeb2840dbd0177e137fa68a7
Outcome: blocked, partial patch. Do not merge this patch as a completed build.

## Completed diagnostic step (H)

- Both adapter diagnostic prefixes now use the existing protocol label. The exception remains UnknownOperation, and the operation/manifest context is unchanged: gateway/engine_protocol/node_graph.py:519-521 and :582-590. The label was already supplied by the description at :441; its shipped value is registry/protocols/node-graph.yml:19.
- The missing graph target uses the generic "binding target missing" prefix. ContractErrors/ManifestError behavior, target_missing, the original path, and the node/input identifiers are unchanged: gateway/manifests.py:804-820. The selected binding is already present in the path supplied at :632; refusal constructs ContractErrors at :120-122 and its JSON Pointer is formed at :46-49.
- Updated the single affected captured message in tests/contract_error_corpus.py:301. The case still exists at :216 and its expected target_missing path remains at :386. No corpus cases were added or removed.

## Pending graph-key step (J), exact blocker

The history roster is NOT changed in this partial patch: gateway/engine_protocol/node_graph.py:71 and :663-673 still use OUTPUT_KEYS. The existing source of truth is registry/protocols/node-graph.yml:34-45, especially fields.key.values at :41.

The allowed tests require no-description construction to keep legacy handles (tests/test_node_graph_adapter.py:255-261 and :582-588). The brief also asks to consider outside callers of the static API. The safe implementation must preserve NodeGraphAdapter._handles(job) and the no-description behavior while using a supplied description's declared keys and preserving order.

An owner question was sent through bridge progress at 05:34:22 UTC and was still unanswered at the final owner-note check. Needed information:
1. The SelectorDescriptor fields/key field representation and its ordered values accessor.
2. The supported loader for the single shipped protocol description, without guessing APIs or loading an unrelated registry.
3. Whether outside callers import OUTPUT_KEYS or call NodeGraphAdapter._handles(job). Keeping the static API avoids the latter migration; an exported derived default can preserve the former if required.

The relevant implementations are outside the immutable read allow-list. No additional source was opened and no interface was guessed. This is an unanswered contract question, not a permission denial or cancellation. No request was sent to the owner.

## Test-first evidence and expected base failures

The new test file was applied before either production file changed. Checks were expressly prohibited, so these are source-based expected failures, not observed test results.

- tests/test_graph_keys_and_labels_from_data.py:44-77: the handle equality at :60 fails on the base because woven and bundles are absent from its fixed roster, while undeclared videos/audio are included. This remains red in this partial patch. The collect assertions at :72-77 protect the already selector-driven result path.
- :80-105 adds five refusal cases: undeclared selector key, missing selectors, ambiguous single selection, unsafe filename, and preview-only output. These protect existing behavior; no new base failure is expected.
- :108-128 preserves default/static handles and the no-description origin/selector state. The explicit-selector-absence assertion at :124 fails on the base and remains red here because the old roster is still used.
- :131-135 checks that malformed enum values are refused by protocol parsing. This protects the existing validator; its execution remains unverified.
- :138-152 adds two adapter diagnostic cases. The label-prefix assertion at :148 fails on the base for both unknown operation and missing manifest.
- :154-169 adds missing-node and missing-input-container cases. The generic message equality at :168 fails on the base for both cases. Error type, target_missing, selected-binding path, original pointer, safe identifiers, no value echo, and source-request immutability are asserted at :164-169.

Case-count change: +12 collected cases in five new test functions (1 + 5 + 1 + 1 + 4). Existing test case counts are unchanged. The budget file was not read or edited.

## Literal count and integration follow-up

This partial H-only production change removes 3 scanned sites: the two node-graph prefixes at gateway/engine_protocol/node_graph.py:521,:589 and the engine-branded prefix at gateway/manifests.py:819. Completing J by removing the roster literal at node_graph.py:71 should remove 1 additional audio site, for 4 fewer in the complete requested build, provided no new literal sites are introduced.

These are source-derived deltas, not executed scan totals. The counting rules are tests/test_domain_literal_ratchet.py:106-147; the equality requirement is :342-350. The lead must reconcile reviewed allowances and lower the budget after integration. That budget edit is intentionally outside this patch, so the existing ratchet equality test cannot be claimed green.

## Exact focused files for the lead

After completing J and reconciling the budget, run:
- tests/test_graph_keys_and_labels_from_data.py
- tests/test_node_graph_adapter.py
- tests/test_binding_descriptors.py
- tests/test_domain_literal_ratchet.py

Also run the existing suite that consumes tests/contract_error_corpus.py and the repository's required full checks. The consumer's filename is outside this read scope and was not guessed.

No tests, lint, type checks, compile checks, ratchet scan, or other repository checks were run. Only bridge reads, patch application, and diff inspection were used. The large root gateway module was not opened. There was no publication, deployment, permission change, Work/Codex task, model API call, or metered tool use.

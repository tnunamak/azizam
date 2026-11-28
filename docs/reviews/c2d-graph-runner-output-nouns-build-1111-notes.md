# Graph runner output nouns

Base: `2e76fa596f9cf63fcaae675300a2bafbc868ae8b`.

- Uses asset_types.kind_names for declared output label/plural, including unknown-kind fallback. Undeclared outputs retain image wording. Only initial letter is lowered for sentence nouns, preserving labels such as 3D model.
- Passes the round's outputs to failure_of. Dropped and wait-exceeded messages change with declared kind; codes/categories/status/retry metadata stay unchanged.
- Complete allowed test file searched before editing: no pre-existing exact dropped/timeout image sentence assertions. Broader tests/goldens outside read scope were not searched. No catalog, OpenAPI, golden, registry or budget edits.
- Added13 cases (10 wording/metadata,2 real _one_round boundary,1 fallback); static collection expectation24->37, functions15->18. Budget unchanged.
- Python AST parsing passed.11 isolated added test cases plus a 3D plural assertion passed on final source, using real scoped pure definitions and synthetic graph_round/client_errors dependencies. A first final-check harness attempt failed on missing AST lineno before execution; corrected harness passed. These are NOT full pytest results.2 boundary cases and full pytest remain unrun.
- Bridge default check: capability_not_available, host_execution, execution=worker; no check process ran. Owner should run pytest tests/test_node_graph_runner.py in the complete checkout. No repository imports outside read scope used for isolated harness.
- Published bytes unchanged by edits; compatibility of out-of-scope goldens cannot be certified here.

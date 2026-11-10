# Resume batch result retention

Base: `5e05a4bf8fea6a5bc38a2306e587a396f47b7479`.
Only the two allowed source files were read. The patch changes the runner and creates the requested test and notes files. The large root gateway module and the test budget file were neither read nor edited.

## Failure and minimal change

- On the base, `gateway/node_graph_runner.py:320-322` skips batching during recovery and creates `Plan(total=1, max_rounds=1)`. Its shared slice at base line 351 consequently retains only the first harvested file. The saved prompt checkpoint is at base lines 411-412; the result stages, storage, metadata and commit are at base lines 281-296.
- The three-line change at final `gateway/node_graph_runner.py:322-324` replaces only the recovery plan's file total with the original input `n`, defaulting to one and clamping to the existing `MAX_FILES_PER_REQUEST` (line 55). It leaves the one-round ceiling and disabled batching intact at lines 320-328. Fresh collection still uses the host plan at line 320.
- The existing slice now caps the resumed files at that request total (`gateway/node_graph_runner.py:354`). The underproduction warning also uses the same total (lines 357-358).
- Recovery still passes the saved prompt to the engine round (`gateway/node_graph_runner.py:391`). The existing contract outputs and limits are passed through at line 392, and actual storage still uses `LimitedAssetStore` and its per-asset limit at lines 474-483. No result-limit enforcement was removed.

## Tests written first

All four tests, including the no-resizing assertion, were applied before the runner change. They follow the existing module's `asyncio.run`, fake objects and monkeypatch style (`tests/test_node_graph_runner.py:81-100,183-190`). The new fixture runs the real `_rounds`, `_collect` and `_one_round` path while replacing engine I/O, job persistence, storage and commit (`tests/test_node_graph_runner_resume.py:64-81`).

Expected base outcomes below are from source tracing, not executed results:

- `tests/test_node_graph_runner_resume.py:95-96`, resumed three-file batch: the exact stored-byte assertion fails, actual `[b"A"]`, expected `[b"A", b"B", b"C"]`.
- `tests/test_node_graph_runner_resume.py:99-100`, fresh three-file batch: expected to pass on the base and remain unchanged.
- `tests/test_node_graph_runner_resume.py:103-105`, resumed video without an `n` input: expected to pass on the base. Returning an extra video protects the one-file default, not merely successful delivery of a single upstream file.
- `tests/test_node_graph_runner_resume.py:108-109`, resumed oversized prompt: the exact stored-byte assertion fails on the base, actual `[b"A"]`, expected the first three files with `b"extra"` excluded.

The shared assertions require exactly one engine harvest with the correct saved prompt, identical output-contract and limit objects, one commit, and accurate returned-count metadata (`tests/test_node_graph_runner_resume.py:83-90`). The fake batch planner rejects any recovery sizing call (line 32). Content comparisons use distinct Python byte strings, so file identity cannot be obscured by generic file-object equality.

Case-count change: **+4 pytest cases**. Existing tests and the budget file are unchanged.

## Verification and lead handoff

No tests, checks, imports, compilation, model APIs, Work/Codex tasks or metered tools were run. The bridge accepted the scoped patches; source and final diffs were inspected. No green-suite claim is made.

Run these exact test files in the lead's approved environment:

```sh
python -m pytest -q tests/test_node_graph_runner_resume.py tests/test_node_graph_runner.py
```

Then run the repository's required aggregate checks before merging. This assignment did not permit reading their configuration or executing them.

Count-source review assumption: the brief names original request input `n`, and the allowed runner already supplies `ctx.inputs` to both the request and host batch planner (`gateway/node_graph_runner.py:307,320`). The patch reads that same input, rather than calling host sizing during recovery. The owner was asked to confirm the count-normalization source through the bridge; no answer had arrived when these notes were prepared. The host implementation is outside this assignment's read allow-list, so the lead should verify that its normalization agrees.

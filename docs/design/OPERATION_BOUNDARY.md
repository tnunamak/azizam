# Operation boundary: one portable path for every engine family (draft 1, 2026-11-05)

Grounding: an internal research note on engine-family coupling (audit of 26 modules, proxy.py inventory of 133 spans), the arbitration foundation (§4.3 operation protocol, §16), and the existing docs ENGINE_CONTRACT, CORE_CONTRACTS and EXTENSION_ARCHITECTURE.

## The defect
The wire calls are mostly inside the adapter package already. The callers are the problem: they prepare graphs, pass node ids, read raw history and name the backend "comfyui". A new engine family therefore needs core edits, however good its adapter is.

## The rule (Meadows level 5)
A caller hands the boundary an offering reference, typed inputs and asset references. It gets back an opaque handle, typed observations and output descriptors. It never sees a graph, a node id, a prompt id or a backend name. Every external effect goes through the operation protocol: committed before dispatch, no blind replay, transport end starts reconciliation and never settles.

## What already exists (reuse, do not rebuild)
- `EngineAdapter` (gateway/engine_protocol/adapter.py): health, inventory, invoke, submit, poll, events, cancel, recover, fetch_assets.
- `jobs.run` (gateway/engine_protocol/jobs.py): the durable submit/poll/fetch loop, which already refuses to resubmit after an ambiguous submit (`submission_unknown`).
- `reviewed.adapter_for_backend`: data selects the adapter.
- http-v1 executor: declarative non-graph engines.

## The missing piece
`NodeGraphAdapter.submit` needs a finished graph (op `graph.submit`). Preparation (portable inputs to graph) happens in callers. Move it inside: the graph adapter takes a `CanonicalRequest` with portable inputs plus the manifest binding, and compiles the graph itself.

## D0: the proof slice (done criteria, report "N of 7")
1. The graph adapter accepts the portable request for one existing offering, `audio.generate`, and compiles the same graph that proxy.py builds today. A parity test compares the two payloads byte for byte over a table of inputs.
2. One outer runner path (`jobs.run` over `EngineAdapter`) runs that offering on three adapters: the graph adapter against a fake graph engine, the http-v1 executor against a fake async job engine, and a fake streaming engine. The same test drives all three.
3. The runner never imports a concrete adapter class, and never reads a node id, prompt id or backend name (an AST test).
4. A lost submit reply ends `submission_unknown`; a duplicate or reordered event never moves a job backwards; a transport close never counts as completion.
5. Renaming the backend id, adapter label and every node id in the fixtures leaves results equal, apart from labels.
6. The live `/v1/audio/generate` route uses the new path behind no flag, with its current request and response contract unchanged (existing audio tests pass).
7. A local fake `smell.infer` offering (vector input, JSON molecule output) runs through the same runner with data plus one adapter and no core edit.

Not in D0: other graph routes, requirements (B2), capability projection (M1), the resource ledger link (comes when the ledger has authority).

## Constraint
proxy.py in the live checkout carries uncommitted edits from 2026-11-04 (kobold removal, Apps page), preserved on branch wip/live-dirty-20261104 (2797f7d44). A proxy.py change cannot fast-forward the live checkout until those are resolved. Criterion 6 waits on that; criteria 1-5 and 7 do not touch proxy.py.

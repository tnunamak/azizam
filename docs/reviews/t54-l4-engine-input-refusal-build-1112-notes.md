# Train 54: bounded engine input-refusal propagation

Assignment: `t54-l4-engine-input-refusal-build-1112-r0`.
Base: `a048a58a12c41cef532ea72a2f95092402c2b10a`.

## Changes

- `gateway/engine_protocol/graph_round.py` retains a parsed, bounded refusal body separately on `RoundFailure`; it never adds that body to exception arguments, text or repr. It uses the existing `UpstreamError.body`, with no transport changes.
- `gateway/engine_protocol/node_validation.py` parses at most 64 KiB of complete UTF-8 JSON. Truncated, oversized, duplicate-key, non-JSON-constant and malformed bodies provide no evidence. The protocol decoder emits only node ID, input name and validation type, with at most 64 facts and 128 characters per identifier. It atomically rejects an unknown or malformed node/error. The supplied real `value_not_in_list` wire type is supported; other validation types preserve existing behavior.
- `gateway/node_graph_runner.py` returns `RunFailed('invalid_input', 'invalid_input', ...)`, the actual 400/422 upstream status and `retryable=False` only when every refusal fact resolves to the same unambiguous public input that this caller supplied. The sentence names the pinned public input, never an engine-provided value, explanation, class name or engine input alias.
- The round snapshots its reverse binding index and caller-presence set before awaiting the engine. Generated values, batching overrides, non-media staging overrides, missing graph targets and changed mapped nodes are excluded from attribution. An unrelated variant edit leaves unchanged mapped nodes eligible.
- Attributed invalid-input failures bypass both variant fallback and model-unload/OOM retry, including an engine excerpt containing an OOM marker.
- Repeated empty rounds use the declared output noun (`audio`, `video`, `images`, or the existing kind-table fallback) instead of always saying `images`.

## Pinned binding and caller provenance: the initially uncertain path

The scoped runner supplies `ctx.pin.binding` in `_collect` (line 346) and passes it into `_one_round` (lines 373-376). The latter builds the immutable attribution snapshot and supplies it to `failure_of` (lines 466-467). No current manifest reload or engine-provided public field name is used.

The allowed source delegates the binding schema and request-context construction to modules outside this assignment. The owner clarified those contracts in note `note_0f22194c-e1bc-4eb8-8abe-7459a3c9fdc2`, at 2026-11-12 17:34:10 UTC, after checking the pinned source:

- `binding['map']` maps a public input name to a nonempty list of `{node, input}` or `{path}` targets. A public input can fan out; two public inputs reaching one node/input target are ambiguous.
- `ctx.inputs` holds caller inputs recorded before graph constants/defaults; `ctx.media` separately holds caller media. Keys in either count as supplied. `ctx.generated` is separate and does not establish caller provenance.

Those owner-supplied facts are the basis for attribution. The defining out-of-scope modules and the large root gateway module were not opened.

Accepted provenance limit: a protocol frontend can translate or fill fields before creating the stored inputs. Such a frontend-filled input counts as caller-supplied in this build, as authorized by the owner note.

## Conservative coverage limits

- Multiple refusal facts must resolve to one supplied public input. Mixed public inputs, mixed caller/default faults, unknown nodes, ambiguous targets and unrecognized validation types retain the prior upstream failure.
- Any path mapping disables attribution for that binding, including a mixed node/path map. A path can alias a known node/input target; this change does not guess or interpret path syntax. Checking only the final value would not establish which public input owned an aliased target. A regression case explicitly covers a path alias alongside a valid node/input mapping.
- A variant that changes any part of the mapped node disables attribution for that node, even if the target value is unchanged. This favors preserving an upstream failure over blaming a caller for a workflow edit.
- A round-level generated or batching override is not attributed to the caller even when its resulting value happens to equal the caller value.
- Only HTTP 400 and 422 qualify. All other statuses retain the existing mapping.

## Verification and unrun checks

Executed source-isolated checks against the exact modified definitions: **66 cases passed, 0 failed**. These comprise **53 new regression cases** and **13 existing failure-word/round-forwarding regression cases**. The temporary local harness runs the regression functions in the scoped test file with declared dependency doubles; it is not a repository pytest run.

The isolated checks cover the exact supplied live HTTP 400 body, the same body with status 422, the `UpstreamError.body` to `RoundFailure.refusal_body` handoff through `graph_round.run_round`, public-name aliasing, safe exception text/repr, malformed/truncated/oversized/duplicate-key/non-JSON-constant responses, atomic mixed-fault rejection, defaults and generated values, staged caller media, ambiguous/path bindings, immutable provenance during an await, changed and unrelated variant edits, OOM retry suppression, variant fallback eligibility, fan-out and declared output nouns.

Dependency doubles are limited to protocol transport/types, jobs persistence, generated-value offsets, shared kind words and the existing fixed public-reason table. The changed parser, decoder, runner failure classification, round exception handoff, attribution and retry decisions execute from the actual modified source. The source-isolated checks do not establish full project import, persistence, live-engine or frontend integration compatibility.

All four changed Python files also passed Python AST syntax parsing. Independent review of the complete implementation and regression diff approved the patch before workspace application.

Repository pytest was not run: neither the system Python nor the primary runtime has pytest installed, and the assignment's exposed check IDs do not themselves document the commands they execute. The lead requested an owner-confirmed offline check mapping; no verified mapping had arrived at the time these notes were written. No opaque broad check, paid check, provider/model call, coding CLI or external engine call was used. No test-budget or reviewed-literal allowance file was read or edited.

The referenced earlier design report returned `message not found`; implementation and tests use this immutable build brief and the owner's clarification, without claiming to have reused that report.

## Wire and manifest literals

These literals remain where their protocol or pinned-manifest contracts are interpreted:

- `gateway/engine_protocol/node_validation.py:64` `error`, `node_errors` - the top-level node-graph refusal envelope in the supplied real response.
- `gateway/engine_protocol/node_validation.py:65` `type`, `prompt_outputs_failed_validation` - require the specific prompt-validation refusal discriminator before extracting facts.
- `gateway/engine_protocol/node_validation.py:72` `errors` - the complete per-node error list, checked atomically.
- `gateway/engine_protocol/node_validation.py:76` `type`, `value_not_in_list` - the input-choice validation type verified by the real response; unknown types provide no attribution evidence.
- `gateway/engine_protocol/node_validation.py:78` `extra_info` - the protocol's structured input-validation metadata container.
- `gateway/engine_protocol/node_validation.py:79` `input_name` - the engine input identifier used only for pinned reverse lookup, never directly for the public sentence.
- `gateway/node_graph_runner.py:183` `map` - the pinned binding-map contract confirmed by the owner.
- `gateway/node_graph_runner.py:192` `path` - a mapping form deliberately excluded from node attribution because path aliasing is unproven.
- `gateway/node_graph_runner.py:194` `node`, `input` - the confirmed node/input mapping target keys.
- `gateway/node_graph_runner.py:221` `400`, `422` - the actual upstream client-refusal statuses eligible for input attribution.
- `gateway/node_graph_runner.py:226` `invalid_input` - the existing public failure code/category requested by the brief; returned with the actual upstream status and no retry.
- `gateway/node_graph_runner.py:427` `inputs` - the graph-node input container named by the confirmed binding write contract; used to verify the submitted target still holds the caller value.

The complete captured response, including its particular node IDs, class name and rejected value, appears only in the test fixture. Production code has no engine, model, workflow or route ID branches.

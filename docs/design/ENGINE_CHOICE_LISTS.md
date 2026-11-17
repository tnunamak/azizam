# Engine-derived workflow choices

`node_info` is not the snapshot store. Successful process entries never expire;
its optional disk files have no schema or timestamp and are keyed by node type,
not service. A failed lookup can return that disk copy without revealing its age.
It cannot establish a fresh, service-scoped last-known-good list.

## Contract

An input keeps its existing `enum` and may add, for example:

```yaml
choices_from:
  node_type: TextEncodeAceStepAudio1.5
  input: timesignature
```

The declaration names a node definition and input, not an engine. The workflow
binding selects the service. Extract the full option list from that input's
node definition, never the truncated `trim_object_info` fixture. Reject missing,
malformed, empty, or duplicate lists; these are failed refreshes, not empty
choices. Stage 1 accepts non-empty lists of unique strings only.

One versioned JSON store at `cache/engine_choices.json` holds entries keyed by
service ID, node type and input, with choices and the successful fetch time.
The path is supplied to the store; tests use temporary paths, never live data.
Private atomic replacement protects the previous file on failed writes. Invalid
or unknown schemas are misses and are not overwritten automatically. Refresh
writers must be serialized across processes; stage 1 does not implement that
lifecycle or concurrent coordination.

Refresh on service ready, an explicit owner action, and when an entry reaches a
bounded age (proposed default 24 hours). Fetch fresh data, bypassing the
process cache; do not mistake its disk fallback for a successful fetch. On
failure retain the last-known-good entry, including when stale, and expose the
failure and age to the owner. A missing snapshot uses the manifest enum; a stale
snapshot remains usable. Validate requests against the selected snapshot, or
against the enum if there is none. An unavailable service must never silently
replace choices with an empty list. Service identity changes need a refresh;
removed services' entries can be pruned separately.

Stage 1 is unwired. The first stages do not change OpenAPI or workflow listing
bytes; neither publishes live choices. A later, explicitly approved publishing
change can expose choices and freshness without changing this validation plan.

## Build slices after stage 1

1. Parse and validate `choices_from` in manifests. Red test: malformed declarations fail while existing manifests retain identical output.
2. Extract full choices from a supplied node definition. Red test: missing or empty options fail without replacing a snapshot.
3. Refresh on service ready and bounded age with serialized writes. Red test: failed fetch retains stale choices and reports failure.
4. Add owner refresh action. Red test: an owner can refresh; unauthorized callers cannot.
5. Validate requests using the service snapshot and enum fallback. Red test: a new engine choice is accepted and a missing snapshot preserves existing validation.
6. Expose freshness and refresh failures to the owner. Red test: stale and failed refresh states are visible while public document and listing bytes remain identical.

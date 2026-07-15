# Authenticated Operation Consumer Contracts

An operation target is typed intent, not a conventional model alias. Its
validated authenticated discovery projection is the complete canonical consumer
construction contract; omission means the operation is unavailable.

The gateway derives `vivid_fish_operation.request_construction` only after the
same logical-target resolution and `CompactionContract` validation used for
admission. Consequently profile filtering, service evidence, generation,
expiry, base-model binding, and duplicate-target rejection apply equally to
discovery and execution. The projection never re-parses raw registry data and
is not a second source of policy.

The versioned object specifies the logical model id, exactly two scalar-text
messages (`system`, then `user`), `stream: false`, the canonical `max_tokens`
field plus default and maximum, the final UTF-8 byte ceiling, conservative
(not exact) token semantics, and the exact operator-declared
`final_reasoning_control` mapping. Consumers supply only the two text values
and may construct one strict request solely from this object.

Before a control mapping can enter the validated contract, the gateway rejects
non-JSON values, non-finite numbers, ambiguous/non-string keys, and
credential-bearing keys or common credential-like values. It freezes the
normalized value and returns a fresh deep copy for every discovery projection.
The gateway does not log these mappings, constructed requests, prompts, or
credentials. This boundary is generic: it names no engine, model, or consumer.

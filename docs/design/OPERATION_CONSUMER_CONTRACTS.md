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
(not exact) token semantics.
Consumers supply only the two text values and may construct one strict request
solely from this object.

`final_reasoning_control` is deliberately absent from discovery. It remains an
internal, evidence-bound admission invariant: the operation forces its
reasoning default, the configured gateway transform produces the internal
control, and admission compares that result to the service evidence before the
gateway sends the exact admitted bytes upstream. Consumers never send or need
internal reasoning or backend-control fields. This boundary is generic: it
names no engine, model, or consumer.

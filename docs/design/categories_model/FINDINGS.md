# Remaining v3 ambiguities and model choices

1. **Legacy exact input representation (R3).** The design describes the old
   missing-tag bypass and any-tag OR, but does not specify whether an omitted tag
   field, None, and an empty list follow the same old branch. The permitted files
   contain no production evaluator. The reference tag stage treats all falsey
   tag inputs as skipped in legacy exact, and incomplete in legacy gated. An
   empty legacy allow-list imposes no tag restriction. The three-way report
   accepts a separate legacy_grants result for pre-tag checks; it does not
   pretend v3's exact grant schema reconstructs every historic selector.
   These choices require comparison with live plans and call sites by the owner.

2. **Completeness versus no applicable subject (A1, A3, R3).** The text does not
   define the metadata envelope or the result of a row with zero applicable
   members. The model takes a trusted metadata_complete flag for every path;
   legacy mode also requires nonempty tags. V3 category absence is handled by
   the row's missing/unknown semantics, not by globally demanding every category.
   A requirement with no applicable target denies NO_APPLICABLE_SUBJECT.
   A deny with no applicable target is false. An empty in candidate list is
   valid but cannot satisfy a requirement with present values; missing input
   under deny-in remains unknown even with an empty candidate list.

3. **First failure ordering (A25).** V3 requires a first failing rule/member
   without a total precedence order. After identity resolution, the model
   validates all configuration and the complete selected closure, checks the
   completeness gate, then evaluates person/app denies, grants and requirements.
   Plans, rows and closure members preserve input order. Unknown and matching
   denies use that same deterministic order. All operational errors use
   POLICY_UNAVAILABLE. Waiver uses accumulated before a later failure remain
   in its explanation. Reasons include named row and member IDs.

4. **Revision representation and current facts on retained bundles (R2, R4).**
   V3 leaves wire formats and immutable-ID enforcement unspecified. IDs are
   opaque strings with a revision suffix or digest prefix, not verified hashes.
   Dependencies are embedded pinned Service objects; alias strings are rejected.
   The first member is the root release/workflow; optional grant selectors
   match only it. Structural changes, including checkability, need a new
   service revision. History also protects helper revisions across snapshots.
   Retained outputs carry the recorded graph in the current policy snapshot;
   owner fact corrections must update its assertions as well as any live
   service copy. The model does not invent a production fact-registry lookup.

5. **Preview inventory and commit mechanics (A22, A25).** No concrete inventory,
   disk format, locking protocol or failure injection API is specified.
   History expands previews across known people, apps, granted capabilities,
   service alternatives, enforcement boundaries and retained outputs, plus
   explicit metadata/tag fixtures. It states the finite scope and cannot
   enumerate unknown future requests. Deep copies, candidate integrity checks,
   compare-and-swap, pre-ack cache invalidation and interrupted-commit cases
   specify durability semantics in memory, not actual filesystem crash safety.
   Registry is a draft-only composer: reset/undelete/default updates have no
   live effect until effective data enters a previewed, owner-activated snapshot.

6. **Acceptance authentication and provenance formats (A11, A24).** V3 names
   provenance fields but not timestamp syntax, mapping schemas, source
   authentication or how an owner principal is established. The model treats
   actor/owner identities as trusted authenticated inputs, requires every
   provenance field to be nonempty, and requires an explicit mapped value
   for outside evidence. It performs no automatic acceptance, source ranking,
   legal interpretation or time-based expiry. Contradictory assertions remain
   present; retraction/correction is a new owner-previewed policy snapshot.

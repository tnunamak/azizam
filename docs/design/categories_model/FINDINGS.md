# Findings: executable draft choices

This is a reference model of CATEGORIES_AND_PLANS.md, not production authorization.
The source was read in full (168 lines); no other repository files were read.
The 72 named unittest scenarios are the executable discussion points.
Tests are UNRUN. The owner command is:
`python -m unittest discover docs/design/categories_model`

## Decisions needed before implementation

1. **Bundle aggregation is unspecified** (Subjects; Plans). Requirements apply
   to every applicable resolved member, and every accepted value on that member
   must satisfy its row. Denies match any applicable member/value. One classified
   release cannot supply a missing classification for a helper. This conservative
   choice prevents a local component from concealing a cloud dependency, but can
   require more labels than the intended one-card UX suggests.

2. **OR in a row versus multiple assertions** (Plans). OR describes candidate
   rule values, not a choice of convenient assertions. A set category is modelled
   as scalar member assertions from a finite owner-defined vocabulary; contradictory
   accepted values are retained and all must pass requirements. The draft does not
   define scalar-versus-set storage, conflict resolution, source priority, owner
   replacement, or retraction. Callers must replace superseded assertions explicitly.

3. **Unknown never matches conflicts with the missing operator** (Principles;
   Plans). The explicit missing operator is the exception: it matches absence.
   Unknown does not satisfy in, at_most or exists. A deny-missing row intentionally
   denies absence; otherwise absence does not activate a deny.

4. **Missing exceptions override which rows?** (Missing values). Allow applies to
   any otherwise-failing non-missing requirement with no accepted values, including
   exists. It cannot override a known mismatch, explicit missing row, deny, empty
   grant, or evaluation error. Hold behaves similarly but only for output subjects;
   a service-side hold configuration produces an ordinary missing-value denial.
   Whether invalid hold configuration should instead be an evaluation error is open.

5. **Binary access versus held lifecycle** (Outputs). Every evaluation returns
   allow/deny; held output is explicitly deny with status=held. Hard errors, denies,
   missing grants and hard requirement failures precede a pending hold. This is
   essential: an owner release of a hold must not accidentally bypass a hidden deny.
   The lifecycle representation is intentionally separate from content storage.

6. **First failing rule has no defined total order** (Why denied). The model uses:
   validation errors; inherited owner denies; person then app explicit denies;
   inherited review restrictions; person then app grants; route checkability;
   classifier hard failure; ordered requirements; first hold. Category and rule
   identifiers accompany denials. Rows and bundle members preserve input order.
   Validation includes all rules, even rules an earlier denial could make unreachable.

7. **Applicability vocabulary mismatch** (Category; Subjects). Service is not one
   of the five internal subjects. Here service expands to release, route and workflow;
   request and output are separate. The internal short names release/route/workflow
   denote model release/provider route/workflow revision. Identities are opaque exact
   strings; the caller supplies immutable revisions and the selected actual route.

8. **Output rules at dispatch** (Enforcement points). Output-only requirements defer
   until output exists. At release/retrieval/share, the output and original resolved
   bundle are evaluated together under current plans. A row with no applicable
   target otherwise denies. Missing output at a release boundary is an evaluation
   error. Mixed applicability and request-only requirements need further design.

9. **Grant schema and granularity** (Plans). Grants are OR selectors over capability,
   service, release and workflow identities, with AND within a selector. Empty grants
   grant nothing; empty selectors are malformed. Release/workflow selectors match
   identities in the resolved bundle. The draft does not define whether dependency
   grants must be separately authorized or whether matching a helper may grant its
   enclosing service. This model does not claim to settle that security boundary;
   production needs explicit dependency capability/grant semantics.

10. **Intersection cardinality** (Plans). evaluate accepts a sequence of plans and
    intersects all of them; it denies an empty sequence. The caller supplies both
    person and app plans, as all scenarios and change_preview do. There is no implicit
    unrestricted app or person policy. Identity lookup failure in previews denies.

11. **Standing-rule language and authority** (Values and evidence). Rules match exact
    source/category/subject-kind triples; first matching named rule applies evidence.
    Owner edits receive the explicit provenance rule "owner edit". No network source
    identity, signatures, authenticity verification, precedence, or value-transform
    language is specified. This is a trusted input model, not an evidence verifier.

12. **Malformed unaccepted evidence** (Principle 4). It remains inert, including
    unknown categories and invalid values, rather than causing an access denial.
    Accepted assertions require valid types, applicability and a recorded rule.
    Evidence retention/card presentation are outside this pure evaluator.

13. **Requester attributes may restrict but never widen** (Values and evidence).
    Even accepted request attributes are excluded from all positive requirement
    tests and from deny-missing presence tests. Otherwise adding an attribute could
    clear a missing deny and grant access indirectly. They may activate ordinary
    deny rows. Requester-provided grant selector identities are never consumed.
    Request attributes remain out of the production first slice.

14. **Ordered/set/number typing** (Categories; Plans). Ordered vocabularies are
    finite, unique and increasing in restriction. at_most has exactly one bound and
    is only legal on ordered categories; number supports equality membership and
    presence only. Booleans cannot masquerade as numbers; nonfinite numbers fail.
    Open sets, units, ranges, coercions and reordered vocabulary migration are absent.

15. **Classifier coverage and pass meaning** (Outputs). Coverage must match modality,
    language and checked unit exactly; absent coverage, pending, or timeout holds.
    Incomplete configured records and unknown result states are operational errors.
    Version, threshold, timeout behaviour and data destination are recorded, but a
    trusted runner supplies pass/fail; the reference model does not calibrate scores,
    call classifiers, or enforce data-destination consent. Pass does not fabricate an
    accepted category assertion: output requirements still need standing-rule evidence.

16. **Timeout behaviour versus mandatory hold** (Outputs). The draft both records
    configurable timeout behaviour and mandates holding on timeout. Holding always
    wins here. Classifier failure is a hard denial, while pending classification is a
    hold. Languages for nonlinguistic modalities need a documented sentinel policy.

17. **Streaming and fallback resolution** (Outputs; Enforcement points). The selected
    service and all helper services must be checkable. Buffered routes may be marked
    checkable; unbuffered ones are not offered. Dispatch does not demand output
    evidence yet. Aliases/fallback choices must already be resolved by the caller;
    dependency cycles and unresolved helpers fail operationally. This is not a menu,
    router, job dispatcher, or alternate-route selection algorithm.

18. **Hold expiry and owner override** (Outputs). HeldItem requires explicit expiry
    and reviewer identities. Expiry wins, including against owner release. Before
    expiry an explicit owner action can release only a currently held decision,
    never a current hard denial. Caller must authenticate the owner and re-evaluate
    current policy. The text could instead allow post-expiry recovery or broader
    overrides; retention, deletion, review payload minimization and visibility
    authorization require product design. visible_to only models the explicit ACL.

19. **Which inherited facts are restrictions?** (Derived models). An owner-supplied
    category restriction flag identifies legal/administrative assertions. No name is
    special in executable semantics. Accepted restriction assertions retain source
    and original standing rule and become needs_review. Factual judgements are
    dropped; unaccepted evidence is not inherited. The draft needs per-assertion
    restriction typing if one category can mix facts and obligations.

20. **Needs review enforcement is not defined** (Derived models). Any accepted
    inherited review restriction blocks access until explicit owner review/correction.
    This is intentionally conservative; it is not silently treated as a fresh grant
    or as an ordinary factual value. The source does not define reviewer resolution,
    obligation satisfaction or whether some restrictions can remain usable.

21. **Parent deny inheritance** (Derived models). An explicitly attached parent deny
    remains an unconditional review blocker on descendants; a child grant cannot
    cancel it even if child facts no longer match. Arbitrary plan denies are not
    automatically copied into lineage: callers must identify owner lineage denies.
    Multiple parents can be assembled explicitly but lineage discovery, cycles,
    merges, wrong-lineage correction and provenance of that correction are not
    automated. Removing inherited restrictions requires an explicit owner change.

22. **Preview scope and version history** (Change and undo). Preview compares allowed
    dispatch access for an explicit finite list of person/app/service/capability
    requests, using both versions' service registries and plans. It reports exact
    gainer/loser tuples; unchanged denial reasons and release-only changes are not
    access gains/losses. Exhaustive audience discovery, output-release preview and
    policy signatures are outside the function. History snapshots are deep-copied;
    activation and rollback are explicit owner operations, not scheduled actions.

23. **Reset versus deletion** (Bundled defaults). Reset removes only the override,
    not a deletion tombstone. Tombstones survive changed defaults. No API implicitly
    restores deleted data; a deliberate owner operation would be needed. Effective
    registry output is copied so previews cannot mutate defaults accidentally.

24. **Staleness and time** (Bundled defaults). tick only reports whether checked_on
    precedes the supplied ISO date. It never changes assertions, denies or plans.
    The draft specifies no staleness threshold or date validation; "older than today"
    is a simple demonstration, not a freshness policy. Hold expiry denies release
    rather than silently removing a policy restriction.

25. **Release registry lifecycle** (Bundled defaults). Explicit activation and
    rollback are represented by in-memory snapshots. Signed update discovery,
    verification, atomic durable persistence, concurrent edits and crash recovery are
    not implemented; no update is auto-activated. Authenticity must precede use.

26. **First-slice migration is intentionally unresolved** (First slice; Open questions).
    Preserving legacy any-tag behaviour conflicts with interpreting categories as
    cross-category AND if automatically translated. The cloud-plus-teen regression
    demonstrates the desired new rule, not a proof of legacy compatibility. No live
    plans or existing metadata-free request paths were inspected under this scope.
    A production migration must preview actual plans and separately audit all bypass
    paths, direct engine reachability and changed memberships.

## Deliberate boundaries

The reference is standard-library-only and uses no product imports. Names with
domain meaning occur only in scenario values. It performs no authentication,
network traffic, publication, deployment or model execution. Startup port warnings,
UI minimum-disclosure rendering, registry signatures, workflow integration and live
plan migration require separate implementations and evidence. No broad repository
checks were run, and no test pass is claimed.

The owner should run the named suite before accepting this patch. Its purpose is to
make disputed choices inspectable; a passing suite would validate internal consistency
of these choices, not establish that every choice matches the intended final design.

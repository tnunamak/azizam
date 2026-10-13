# Categories and access plans (design v3)

## What categories do (plain English)

A category is a group of models and workflows with one rule: who may see what is in it. You decide what a category means. The gateway does not know what "Adult" or "Experimental" means. It only follows the rule you set. Nothing is filed automatically; an item is in a category only when you put it there.

A category can be visible to everyone, to you alone, or to you and the people and apps on the access plans you choose. Someone who cannot see a category does not find its items in model lists, workflow lists or the showcase, and cannot run them. You see everything when you use your own key or this console. An app you run for other people is treated like any other app.

Items in no category are unaffected. Move an item out of its category and it is available again.

In code: `gateway/category_enforcement.py` reads a category's `visible_to` list (access plan ids) and refuses and hides the items filed in it. `scripts/migrations/seed_categories.py` creates the two starter categories, Adult and Experimental, as ordinary rows the owner can edit on the Categories page. The sections below are the older, finer design for rules about specific people and apps (the page's Advanced section).

## Starter presets

Starter categories are bundled data in `manifests/categories/<id>.yml`: id, label,
one-line meaning and `visibility` (`owner` or `everyone`). `gateway/category_presets.py`
lists and validates them. The gateway sends them with the catalog
(`GET /admin/api/categories/services`, key `presets`). The page offers them under
"Start from a preset"; adding one writes the same rows as the Add category form,
so it stays fully editable and deletable. `seed_categories.py` adds the same list.
No category id or label is in code.

Shipped: Adult and Experimental, both owner only at first.

Prior art (from the divan corpus; nothing re-researched):

- Plex, Jellyfin and Emby let the owner label content and hide it from accounts
  that lack the label. Plex ships a few presets (younger child, older child,
  teen) and owner-written labels; denied items are hidden, not greyed. Jellyfin
  gives a new user all libraries unless access is explicit, so presets here
  start owner only.
- SwarmUI documents a "Spicy" user and a "Safe" user through a model deny list
  by role. Open WebUI grants model visibility to users and groups; a restricted
  model is unavailable even through a direct link. Civitai defaults `nsfw` to
  false. Each ships adult or safe handling and lets the owner add more.
- Apple Screen Time and Google Family Link restrict by categories the parent
  picks. The corpus covers them for supervision, not for content labels, and
  Immich only for per-user libraries. They give no more than this lesson: a
  preset is a shortcut to a rule the owner can change.

Not shipped, because the one visibility rule cannot express them:

- Kids or Family-friendly needs an allow list (kids see only this). The visibility
  rule only hides an item from people who are not listed. The Advanced per-person
  rules cover that case.
- Costs money: an access plan already grants paid providers. A second switch for
  the same fact could disagree with the plan.

Corpus entries: `local-ai-gateway/owner-defined-dimensions-over-imported-evidence-…`
(household section: Plex, Jellyfin, Emby; SwarmUI, Open WebUI and Civitai sections),
`…/administrator-content-reach-differs-across-14-products-…` (Immich),
`…/no-surveyed-product-gives-a-supervisor-every-private-creation-…` (Screen Time, Family Link).

Status of the design below: validated 2026-10-10 and implemented as owner-only category rules (the page's Advanced section). The plain categories above were added 2026-10-13.

Evidence, all in divan `local-ai-gateway/`:
- `owner-defined-dimensions-…`: nine prior-art reports, 1,082 claims.
- `red-team-keeps-azizams-owner-authored-…`: two red-teams of v1, with
  corrections appended to the entry above.
- Round-2 red-team of v2, a resolution report (rules A1–A25, R1–R4) and
  claim verification (`categories-validation-…`, filed with this version).

The executable reference model in `categories_model/` encodes these rules
as scenarios. It checks internal consistency, not product behaviour.

## Problem

An owner wants rules like "kids get general-audience services only", "the team
plan uses nothing non-commercial", "no route that may train on our data".
At the time of this design, a plan restricted capabilities, models, workflows,
providers and four fixed tags. A tag allow list passes if any tag matches. There is no tag deny.
The design review found 15 of 24 policy-request call sites passed no tag data,
so the old tag check was skipped there. The gateway must not learn what "adult" or "non-commercial"
means (`ENGINE_KNOWLEDGE_BOUNDARY.md`): the owner, or a bundled default the
owner can edit, supplies that meaning as data.

## Principles

1. Meanings are data; semantics are code.
2. Deny by default. A request runs only if every attached plan grants it and
   nothing blocks it.
3. A deny that cannot be shown false blocks (R1). Unknown never lets a request
   through.
4. Outside metadata is evidence. It never grants by itself.
5. Nothing widens access without an owner action that was previewed.
6. One evaluator serves runtime, preview and "why denied".

## Vocabulary

- **Category**: an owner-defined dimension with a finite set of values,
  immutable IDs, a label, and the subject kinds it applies to. The first slice
  supports set categories only. Ordered, number and boolean types are
  deferred (A14).
- **Subject kinds**: model release (digest or `repo@revision`), provider
  route, workflow revision. Request and output subjects are deferred.
- **Service revision**: what people pick (an alias or a workflow), pinned to
  one immutable, reviewed dependency closure. The closure is the root plus
  every route and helper it may call, with exact revisions (R2). A change to
  the closure is a new service revision.
- **Assertion**: an accepted value on a subject revision, with provenance:
  source reference or owner origin, observed_at, accepted_at, accepting owner,
  mapping version (A11).

## Evidence

- Only an owner edit, or the owner accepting identified evidence, creates an
  accepted assertion. There is no automatic acceptance in the first slice.
- Imported values (manifest `license`, Hugging Face, Civitai, provider pages)
  are shown on the card as evidence with their source and date. A raw value
  maps to a category value only through an explicit owner mapping.
- A subject holds a deduplicated set of accepted value IDs per category.
  Contradictory accepted values stay visible until the owner retracts one in a
  versioned correction. A new assertion never silently replaces an old one (A2).
- Malformed evidence that was never accepted is inert. A policy that references
  an unknown category or value is a configuration error and cannot activate.
- Licence facts keep the exact licence version and text reference. Commercial
  rights are separate values (weights use, output use, redistribution), not one
  switch. Mapping a licence is the owner's decision, not legal advice.
- Ratings keep separate fields for a classification derived from content, a
  creator or owner override, unrated, and enforcement state.

## Plans

```yaml
plan: kids
grants:
  - {capability: image.generate, service: svc-sdxl@r7}
require:
  - {id: kids-audience, category: audience, in: [general]}
  - {id: kids-data, category: data_handling, in: [reviewed-no-training]}
deny:
  - {id: kids-cloud, category: runs_where, in: [cloud]}
```

- **Grants**: each selector names an exact capability and an exact root
  service revision, optionally a release or workflow revision. Fields AND,
  selectors OR. A selector never matches because a helper inside the closure
  has a matching ID. A new service revision needs a new grant (A9).
- **Require**: `in`, `exists`, `missing`. Rows AND. Values in one row OR. An
  `in` passes only when the accepted set is non-empty and every accepted value
  is in the row's candidates (A2).
- **Deny**: `in`, `missing`. Any matching value blocks.
- **Missing values** (A3): no accepted value on an applicable subject.
  `missing` is true, `exists` is false, `in` is unknown. A requirement that is
  unknown fails. A deny that is unknown blocks with `DENY_INPUT_UNKNOWN`.
- **Waiver** (A4): `missing: allow` names one requirement row and waives only
  that row's missing value. It never waives a known mismatch, a deny, an
  unknown deny input, a missing grant or an error. Each use is shown in the
  explanation. The default is no waiver.
- **Bundles** (A1): every applicable member of the selected closure must pass
  every applicable requirement; a deny blocks if any member matches or is
  unknown. Alternative routes are evaluated as separate bundles and never
  borrow each other's facts.
- **Identity** (A10): every protected request resolves one authenticated
  person and one authenticated app. Every plan attached to either must grant
  and pass. A missing identity, an empty plan list or a failed lookup denies.
  There is no implicitly unrestricted app.
- **Errors**: evaluation or configuration errors deny with
  `POLICY_UNAVAILABLE`, never with a content reason.

Prior-art note from verification: composition rules differ between systems
(Plex combines rating and label allows additively; AWS unions identity
grants within an account and intersects boundaries; Cedar skips policies that
error). Azizam's intersection, deny-wins and deny-on-error are deliberate
choices, not an industry standard, and are covered by tests.

## Enforcement (R4, A17)

Policy is checked on the complete selected execution graph before every
external action:
- after alias, wrapper and fallback resolution, and again on any route,
  helper or revision change, and on every retry;
- at job dispatch and at output release, retrieval, download and share, using
  the bundle recorded with the output and the current policy;
- before sending anything to a helper or classifier (sending is itself an
  action).

Unresolved aliases, dynamic dependencies and cycles deny. A route whose
enforcement cannot happen before its effect is not offered. Every request
path is listed in a path inventory with its enforcing boundary and a denial
test, or is disabled with a visible reason. The inventory includes every
call site that passes no capability metadata today.

Request-side and response-side inspection are separate. A request check
never forces the response to be buffered. Cached responses are keyed to the
policy snapshot, so a policy change invalidates them.

## Preview, versions and undo (A22, A23, A25)

- **Preview as**: pick a person and an app; see each service and route they
  can use and, for each denied one, the first failing rule and member.
- **Change preview**: every security-relevant edit, default reset, undelete or
  restore produces a candidate snapshot and a diff against the active
  revision: who gains, who loses, and which reasons changed, over the known
  people, apps, capabilities, service revisions, routes and retained outputs.
  It never claims to cover unknown future requests.
- **Activation**: the candidate activates atomically only if its base
  revision is still active (compare-and-swap). Caches are invalidated before
  activation is acknowledged. A failed or interrupted activation leaves the
  last committed version live. With no valid committed snapshot, protected
  access denies.
- **Undo**: restore copies an old snapshot into a new candidate, previews it
  and activates it as a new version. It cannot undo disclosures that already
  happened. Restored snapshots are revalidated, so an unsafe legacy gate
  cannot come back through restore.
- **Defaults**: effective configuration = bundled read-only base + owner
  overlays − tombstones. Reset removes an overlay; undelete is a separate
  previewed action; a defaults update never removes a tombstone.
- **Time** (A24): evidence age is shown, never acted on. No timer changes
  access in the first slice.

## Migration (R3)

Existing plans run in an explicitly named legacy mode that keeps any-tag OR
semantics. Two changes are disclosed, not described as preservation:
1. A metadata-completeness gate on protected paths. Paths that reach policy
   without metadata are blocked until they carry it. This closes the
   missing-tag bypass.
2. Conversion of a legacy plan to category rules is a per-plan, previewed
   owner action.

The migration report compares three results over live plans and adversarial
fixtures: legacy exact, legacy with the completeness gate, and v3. Fixtures
cover missing, None and empty tags; allowed and disallowed tags together;
unknown categories; multiple assertions; person and app mismatch; empty
grants; route changes; helpers; retries; cache, output and share paths.

Run `scripts/categories-migration-report` against a private database snapshot.
The [report guide](CATEGORIES_MIGRATION_REPORT.md) documents its deterministic
conversion, static metadata call-site inventory and finite scope.

## Bundled defaults

Shipped with each release as read-only base data: a few categories (audience,
licence use, data handling, runs where), copyable starting plans (kids,
teens, adults, guest, team member, client), and a licence table (licence id
and version → conditions, with source link and observed date). No remote
discovery or auto-update in the first slice. A future remote bundle must
verify against an out-of-band trusted key before it can even be proposed;
a valid signature authenticates bytes, not truth, and never activates or
grants.

## First slice (all items are release gates)

1. Authorization kernel: immutable IDs, set categories, accepted-value sets,
   release/route/workflow applicability (A7, A14).
2. Owner edits and owner acceptance with provenance; read-only defaults,
   overlays, tombstones, reset and undelete.
3. Exact grants; person and app intersection; require/deny/waiver rules above.
4. Pinned bundles, separate route evaluation, re-authorization at each
   boundary; unsupported graphs deny.
5. One evaluator for runtime, preview-as and "why denied", with stable reason
   codes.
6. Durable snapshots, change preview, compare-and-swap activation, cache
   invalidation, crash-safe commit, restore as new version.
7. Path inventory with a denial test per path, including today's
   metadata-less call sites.
8. Legacy mode, completeness gate and the three-way migration report on live
   plans.

Deferred until a concrete owner task needs them: output categories and
classifiers, holds, lineage and derived-model restrictions, request
attributes, ordered and number categories, remote registry updates, delegated
admins.

## Open questions for the owner

- Which starting plans to ship (needs real owners to try them).
- Whether any current client relies on a path that the completeness gate will
  block (the migration report will list them before activation).

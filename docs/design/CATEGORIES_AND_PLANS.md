# Categories and access plans (design v2, draft)

Status: draft for validation, 2026-10-10. Not implemented.
Evidence: divan `local-ai-gateway/owner-defined-dimensions-…` (nine reports) and
`local-ai-gateway/red-team-keeps-azizams-owner-authored-…` (two red-teams). This v2
applies the red-team: keep the core, cut owner chores, add preview and rollback.

## Problem

An owner wants to say things like "kids get general-audience image models only",
"nothing non-commercial in the team plan", "no route that trains on our data".
Today a plan can restrict capabilities, models, workflows, providers and a fixed
set of four tags. A tag allow list matches if any tag matches, and there is no
tag deny. The gateway must not learn what "adult" or "non-commercial" means:
that knowledge is data the owner (or a bundled default) supplies
(`ENGINE_KNOWLEDGE_BOUNDARY.md`).

## Principles

1. Meanings are data; semantics are code. The owner defines categories and
   values. The engine owns types, applicability and rule evaluation.
2. Deny by default. A request runs only if a rule grants it and no rule denies it.
3. Unknown is not a match. An item without a value for a category a rule reads
   does not match that rule. This is engine behaviour, not an owner setting.
4. Outside metadata is evidence, never authority by itself.
5. Nothing widens access without an owner action or a standing owner rule that
   names the source. Every change records which rule allowed it.
6. The owner can always see effective access, preview a change and undo it.

## Data model

### Category (dimension)
```yaml
id: maturity
label: Audience
applies_to: [service, output]      # see "Subjects"
type: ordered                      # ordered | set | boolean | number
values: [general, teen, adult]     # ordered: later is more restricted
```
Bundled categories (defaults, editable): audience, licence use, data handling,
cost class, runs where (local / cloud). Owners add their own.

### Subjects
Internally the engine identifies five kinds of subject exactly: model release
(by digest or `repo@revision`), provider route, workflow revision, request,
output. The owner sees one card per **service** (what people pick: a model
alias or a workflow). A service card resolves to its release, route(s),
workflow and dependencies; its category values are the values of that
resolved bundle. Per-file and per-request labelling is not an owner task.

### Values and evidence
Each value on a subject is an assertion: `{category, value, source, rule}`.
- `source`: owner, bundled default, import (Hugging Face, Civitai, manifest),
  classifier, request attribute.
- Owner edits apply at once.
- Other sources apply only through a **standing rule** the owner set once,
  e.g. "accept licence ids from manifests", "accept classifier output for
  audience on outputs". The applied assertion records that rule.
- Unaccepted evidence is shown on the card ("Civitai says PG-13") and never
  affects access.
- A request may carry attributes (e.g. app-declared purpose) but can never set
  a value that grants access.

### Plans
```yaml
plan: kids
grant:  [{capability: image.generate}, {capability: chat}]
require:                                # all must hold (AND across categories)
  - {category: audience, at_most: general}
  - {category: runs_where, in: [local]}
deny:
  - {category: data_handling, in: [may-train]}
```
- `grant` lists what the plan can do (today's intents, models, workflows).
- `require` rows are AND across categories; values within one row are OR.
- `deny` wins over everything.
- Person plan and app plan both apply (intersection).
- Operators: `in`, `at_most` (ordered only), `exists`, `missing`.
- Evaluation error (unknown category, type mismatch, broken data) denies with
  reason "policy could not be evaluated", never "forbidden content".

### Missing values
A `require` row on an item with no accepted value does not match, so the
request is denied with "not reviewed for Audience". Exceptions are explicit
and rare, set on the plan per category: `missing: allow` (e.g. adults allow
unreviewed audience) or `missing: hold` (only for outputs, see below). The
owner never has to answer a missing-value question to get a working plan:
bundled presets carry sensible exceptions.

## Outputs and classifiers

A classifier is a configured service with a coverage record: version,
modalities, languages evaluated, unit checked (whole image, text chunk),
threshold, timeout behaviour, where it sends data. Its output becomes
evidence on the output subject through a standing rule.

A plan may set `outputs: check_before_release` for a capability. Then:
- the checked unit is released only after a pass;
- if the classifier does not cover the modality or language, or times out,
  the output is held with a visible status, not released;
- a route that cannot be checked before release (e.g. unbuffered streaming)
  is not offered under that plan;
- held items expire after a set time and can be released by the owner;
- the reviewer sees the minimum needed; who can see what is stated.

## Derived models

Restrictions that are legal or administrative (licence conditions, an owner
deny) carry from a parent release to derived releases (quantized, merged,
fine-tuned) as "needs review". Factual judgements (audience, quality,
privacy) do not carry. A derived item's grant never cancels a parent's deny.
Wrong lineage can be corrected by the owner.

## Enforcement points

Policy is checked on the request **as dispatched**, not on the menu:
- after alias, wrapper and fallback resolution, including helper calls
  (e.g. a workflow that calls a cloud prompt expander);
- at job dispatch and at output release (plan or membership may have changed);
- on output retrieval and sharing URLs.
Startup self-check warns when engine ports are reachable without the gateway.

## Effective access, change and undo

- **Preview as**: pick a person or app; see every service they can use and,
  for each denied one, the first failing rule in plain words.
- **Change preview**: before saving a plan, category value or registry
  update, show who gains and who loses access to what.
- **History**: every policy change is a version; one click restores the
  previous version.
- **Why denied**: every denial carries the rule and category that failed.

## Bundled defaults (registry)

- Shipped with each release as read-only data: categories, presets, a licence
  table (licence id → conditions, with source link), and a small set of facts
  about common models keyed by exact identity, each with source URL and date.
- Owner overrides sit on top; "reset to default" removes an override; deleted
  defaults stay deleted.
- Updates arrive with releases. An optional update check only *discovers* a
  newer signed snapshot and shows its effect (change preview); activation is
  one owner action, atomic, with rollback to the last good snapshot.
- Facts carry `checked_on`; old facts show as possibly stale. A timer never
  removes a deny.
- Presets are starting points the owner copies: kids, teens, adults, guest,
  team member, client. They are composable choices about audience, cloud use,
  spend and output checking, not organisation types.

## Out of scope for the first slice

Lineage automation, classifier integration, registry update checks, request
attributes, delegated admins. Each waits for a concrete owner task.

## First slice

1. Tag deny list and `missing` semantics on today's tags (existing plans keep
   their behaviour; verified by an evaluation diff over live plans).
2. Owner-defined categories replacing the fixed tag tuple, on services.
3. Preview-as and why-denied.
4. Licence as the first bundled category, fed from manifest `license`.

## Open questions

- Which existing request paths reach policy without service metadata (today
  `capability_tags=None` skips the tag check)? Needs an audit before slice 1.
- Ordered categories vs sets: is `at_most` worth its complexity?
- Presets: which 4–6 starting points do real owners pick? Needs user testing.

# Operation catalog (code to data phase 2): design draft 2

Status: draft 2, 2026-11-13. Draft 1 was red-teamed (`docs/reviews/c2d2-opreg-redteam-dot-1113.md`, verdict "build
after named changes"); draft 2 takes every P0 and P1 finding. Measured inputs: the operation inventory at `f09015d73`
(`scripts/research/op_inventory.py`: 230 vocabulary words, 144 operation words, 125 closed sets, 29 if/elif chains,
96 co-mappings, 335 implicit-coupling hits) and model judgments of 545 candidate word pairs.

## What changed from draft 1, and why

Draft 1 made every spelling an alias in one namespace that resolves to one operation. That rule is wrong, because
many current mappings are not equivalences. Each is a separate, deliberate fact:

- `rerank` is its own operation, but it is **admitted** under the `text.embeddings` intent, so that a key allowed to
  embed may also rerank (`registry/protocol_frontends.yml:435-447`). Making `rerank` an alias of either side changes
  who may do what.
- `videos.edit` is admitted as `video.edit` but runs under the `video.remix` runtime policy (`:428-433`).
- `image.edit` has a public route, but clients cannot submit it as a media job (`media_job_intents.yml:11`), so
  "submittable = has a public route" would widen the job API.
- A service's offering task restricts what it may **execute** (`dispatch.py:85-91`); a family match must never grant
  every operation in the family.

The measured data agrees. With usage context, a calibrated judge model scores 68 pairs as "same" (score >= 1.5, the threshold that made
0 wrong merges on 35 hand-picked pairs). Those 68 mix four relations: true spellings of one operation
(`audio.generate`/`audio_generate`), family to operation (`image`/`images.generate`), operation to policy protocol
(`chat.completions`/`openai.chat`), and plain errors (`chat`/`decision.systemone`, `images.content`/`openai.images`).
One equivalence relation cannot hold all four.

Also corrected: draft 1 said service capabilities are "free, unchecked". For protocol engines that is false at
`f09015d73`: a task the installed description does not declare is refused on write (400 `invalid_engine`, field
`capabilities`), and nothing is stored. Scenario S7 now asserts this (passes).

## Design

### One catalog of operations, plus typed relations

`registry/operations.yml` (schema next to it) defines **operations** and the **relations** that today live in
separate tables and code maps. Nothing is an untyped alias.

```yaml
schema_version: 1
operations:                       # a typed unit of work; the only place an operation id is made
  - id: image.edit
    family: image                 # display family (today's capability_words.capabilities row); presentation only
    request: {schema: openai.images.edit.request}    # reference to a full request schema, not a modality list
    result:  {schema: openai.images.result}
    submittable_as_job: false     # explicit, never derived from a route
relations:                        # every non-identity mapping, each with its kind; one row per fact
  - {kind: spelling,  context: catalog,  word: images.edit,  operation: image.edit}
  - {kind: spelling,  context: legacy,   word: image_edits,  operation: image.edit}
  - {kind: binding,   operation: image.edit, method: POST, path: /v1/images/edits, representation: multipart,
     modes: [sync]}
  - {kind: admission, operation: rerank,     intent: text.embeddings,  why: "a key allowed to embed may rerank"}
  - {kind: runtime_policy, operation: video.edit, policy: video.remix}
  - {kind: receipt,   operation: rerank,     intent: rerank}
  - {kind: catalog_group, group: videos,     operations: [video.generate, video.edit]}
```

Rules:

1. **Relation kinds are closed and typed**: `spelling` (with the resolver context it belongs to: wire, catalog,
   legacy, display), `binding` (method, path, representation, allowed modes; several per operation), `admission`,
   `runtime_policy`, `receipt`, `catalog_group`, `family`. A reader asks for one kind in one context. There is no
   context-free `resolve(word)`.
2. **No relation widens execution.** Family and catalog-group membership never imply that an engine serving one
   member may serve another. Only an offering task or protocol `serves` grants execution, and it names exact
   operation ids.
3. **Kinds of data stay three separate fields**, not one closed list: the request/result **schema** (for example
   prompt, messages, query+documents, an embedding vector with shape and dtype), the **modality** used for model
   metadata (text, image, audio, video, mesh, embedding, unchanged from today), and the stored-asset **media type**
   (`asset_kinds.yml`, which keeps `model` -> mesh and keeps unknown MIME kinds working). A mask is an image
   parameter with a role, and a voice reference is an identifier, not an audio blob.
4. **Unknown spellings:** the current behaviour stays (`capability_ids.py:331-334` lets an unknown word stand for
   itself; protocol engines already refuse undeclared tasks). New refusals come only after shadow measurement
   shows how many stored records they hit, in their own versioned train.
5. **Owner-extensible later, not now.** Owner operations (the smell test's `smell.infer` as a typed row) come after
   the readers move, with reserved shipped namespaces and alias collision checks under every legacy normaliser
   (`models_catalog.py:123-127` folds case and underscores; `capability_ids.py` only trims).

### Migration (compatibility first; behaviour changes are separate, named trains)

1. **Generate, then prove per consumer.** A script builds `operations.yml` from today's tables. For each legacy
   table and code map, a generator rebuilds that table's exact reader shape from the catalog, and a test asserts
   it is equal to the current table, key by key and in order. This is one agreement test per consumer, not one
   global resolver test. A mismatch is decided as data (a relation row), with the judge's relation-type label as input.
   Nothing reads the catalog yet.
2. **Move readers one group per train.** Each reader switches from its table to the generated shape. Each train is
   byte-identical on the goldens: OpenAPI pin, models list (with order), legacy wire, receipts (including old
   receipts read back), metadata enum order, catalog grouping and example order, usage keys, and a policy matrix
   of allowed **and denied** cases. A table is deleted in the train that moves its last reader.
3. **Behaviour changes, each its own train:** shadow-validate unknown words, then refuse them; owner operations
   route; typed declared-job outputs (joins W3).

### What the judge model does

It does not merge spellings. It labels each candidate pair with one relation kind (a Choice over spelling-of-same,
family-membership, binding, admission/policy projection, different), calibrated first on hand labels that include
the hard cases above. Its labels sort the agreement-test mismatches in step 1; a maintainer decides each one.

## Expect it to work because

Step 1 changes no behaviour: the catalog is generated from the tables it replaces, and the per-consumer agreement
tests prove that each reader would get exactly the shape it gets today, so the deliberate non-equivalences
(rerank admission, video remix policy, edit not submittable) are kept as rows instead of being normalised away.
The likeliest wrong assumption is that every legacy table can be regenerated from one catalog with a small number
of relation kinds. Evidence so far: the red team's 15-row conflict ledger fits the seven kinds above with no new
kind. It is not yet proven for the code maps inside `proxy.py`, which the red team could not read. Check before
building: write the generator for the four largest shapes (capability_words, local_capability_rows,
media_job_intents, protocol_frontends intents) and count the rows it cannot express. Confidence: 65% that seven
kinds suffice; 80% that steps 1-2 land with no wire change, because the goldens cover the wires.

## Open questions (red-team recommendations adopted)

1. Public operation ids: later, as a new versioned field; receipts keep intents.
2. Owner kinds: owners may add schemas and parameter roles over shipped storage primitives, not new storage kinds.
3. Categories: label typed targets (models, workflows, later operations); never imply access or execution.

## Capability display contract gate (2026-11-15)

Status: proposal only against pinned input
`3a91ecd4f6e44271100dd6128735d98d88b99643`. No schema, runtime reader,
shipped data, generated output or test-budget change is authorized by this proposal.
This section does not select an operation for a family.

### Field ownership and projected reader shape

The current source of truth remains `registry/capability_words.yml` until a
separate data-population and reader-migration slice is accepted. Proposed future
ownership is ONE closed `display` payload on `catalog_group`, not an arbitrary
metadata bag. It would describe a display family whose known exact members are
the group's existing `operations`. The owner must approve this identity and
ownership decision before implementation.

| Current field | Meaning and cardinality | Proposed future source | Read projection / validation |
| --- | --- | --- | --- |
| id | One display-family identity, not an operation | catalog_group.group; display uses that same key | Group keys are unique across all catalog_group rows, with or without display; no implicit operation alias |
| label | Presentation title | display.label | Required nonblank string; ordered labels map |
| member_label | Presentation member title | display.member_label | Optional string; existing fallback to label preserved |
| task_name | Presentation task title | display.task_name | Optional string; fallback to effective member_label preserved |
| models_explainer | Presentation explanation | display.models_explainer | Optional nonblank trimmed string; sparse ordered map |
| try_help | Presentation help | display.try_help | Optional nonblank trimmed string; sparse ordered map |
| request_noun | Presentation request wording | display.request_noun | Optional nonblank trimmed string; sparse ordered map |
| summary | Presentation summary | display.summary | Optional nonblank string; sparse ordered map |
| display_order | Presentation rank, distinct from relation order | display.display_order | Nonnegative integer, unique across declared display families; preserve source-order default |
| icon | Reference to another display family's icon identity | display.icon | Validate against declared display families, including forward refs; default to own group |
| hidden_by | Ordered suppression references, not execution exclusions | display.hidden_by | Ordered family-reference array; validate every target, return tuple |
| alias_hides_declared_names | Presentation of an alias row's title | display.alias_hides_declared_names | Boolean; true values project to a frozen family set |
| recommended_alias | Recommendation with explicit target cardinality | display.recommended_alias = {word, target_kind: group} | Nonblank word; target is this entire group, never a spelling or permission; sparse ordered recommendation map |
| policy_protocol | Family-level policy hint; no per-operation authorization evidence | display.policy_hint | Optional opaque non-whitespace string, projected separately as family hints; NEVER synthesize runtime_policy rows |
| operations | Exact known members, not all possible selector matches | Existing catalog_group.operations | Ordered tuple of declared operation ids; no admission/execution derivation |

The proposed payload has a fixed property allow-list and
`additionalProperties: false` at every object level, including recommendation.
It does not contain paths, modes, serves, admission, execute, or offering tasks.
Open prefix selectors are not closed membership lists: retain them separately in
their existing authority or mark their conversion unresolved. Do not fill gaps
by selecting the first known member or fanning hints out over members.
No fallback reader may consult both old and new authorities.

Projected readers preserve the current tuple-of-maps boundaries rather than
inventing a global resolver: labels and policy hints separately; icons,
recommendations and summaries separately; suppression tuples separately from
classification rules; display order; alias-name hiding; member/task titles; and
explanation/help/noun maps. `protocol_words`, `route_intents`,
`type_capabilities`, `audio_rule`, `modality_words` and
`media_type_modalities` are not fields of this proposed display payload.
Their migration is not this contract. Unknown-word passthrough and
consumer-specific normalization remain unchanged.

### Evidence against mechanical conversion

- `capability_words.yml:17-26` describes image generation AND editing with one
  recommendation and one policy hint; its protocol map includes both operations.
  `models_catalog_operations.yml:29-43` distinguishes their selectors and examples.
  A recommendation called image is not evidence for one global executable target.
- `capability_words.yml:27-34` supplies a video recommendation and family hint.
  `models_catalog_operations.yml:80-94` includes a two-prefix videos group as
  well as separate generation/edit rows. A prefix selector is not a proof of a
  complete finite operation set.
- `capability_words.yml:35-80` has several distinct audio-related families
  sharing a policy hint; the audio family also has ordered suppression and an
  icon reference, and lacks a recommendation. Neither sharing a policy string
  nor omitting an alias makes these families operation synonyms.
- `gateway/capability_ids.py:231-237` returns separate maps, tuples and a
  frozenset; `:248-280` exposes distinct presentation/classification readers.
  Existing icon and hidden_by validation checks capability references
  (`:171-179`), not operation references.

### Single-operation and multi-operation examples

These synthetic rows are proposals, not shipped data or executable aliases.
Assume declared operations specimen.render and specimen.revise.

A confirmed one-operation display spelling remains the existing relation:

```yaml
kind: spelling
context: display
word: specimen-render-only
operation: specimen.render
```

This is a context-bound spelling, not an admission grant. A consumer may choose
its own normalization; the catalog exposes no context-free resolve function.

A different recommendation may represent both operations:

```yaml
kind: catalog_group
group: specimen-family
operations: [specimen.render, specimen.revise]
display:
  label: Specimens
  display_order: 0
  icon: specimen-family
  hidden_by: []
  recommended_alias: {word: specimen-choice, target_kind: group}
  policy_hint: policy.specimen
```

There is no spelling, admission or runtime_policy row implied by this group.
Trying to use `operation: specimen-family` as an executable spelling is rejected
because the target is not a declared operation. Trying to put an operations list
on spelling is rejected by its closed schema. Two spelling rows for the same
word/context with different targets are rejected by unique_spelling_in_context.
A consumer must not reduce specimen-choice to either member on its own.

### Why the schema-only implementation stops here

The current schema can describe the payload shape but cannot validate dynamic
cross-row family references. `gateway/operation_catalog.py:164-191` checks
operation targets and spelling uniqueness; it does NOT maintain a declared
display-family index, validate icon/hidden_by against that index, or reject two
different catalog_group definitions with the same group key. Its earlier
duplicate-row check compares entire rows, so conflicting definitions differ
and escape it. Deep freezing (`:39-45`) prevents mutation AFTER acceptance;
it does not prove semantic references are valid.

Consequently the required unknown-family and conflicting-group tests need
generic loader validation changes outside this slice's three change files.
Adding a schema-only payload would leave precisely those invalid rows accepted.
No schema change or deliberately failing implementation test is landed here.
This follows the contract gate's explicit stop condition, not a request to widen
permissions silently.

Decision for the submitting thread: approve or reject ONE display payload on
catalog_group as the future sole presentation source (including group-target
recommendations and inert policy hints), with unique declared display-family
keys. If approved, issue a separate slice that can change generic loader
validation, schema and synthetic tests together. Alias cardinality of shipped
rows and any actual runtime-policy mapping still require per-row evidence before
population. Alternatively keep presentation as a separate derived view and
specify its validated family registry; do not implement both as authorities.

### Red-first acceptance plan for that separate slice

Use two synthetic operations in family specimen, with no shipped ids and no
subprocess, generator, script, dispatch, policy evaluation or engine branches.
All acceptance goes through `load()`, not jsonschema alone. These are proposed
future node ids, NOT tests added or claimed passing in this documentation patch:

- `tests/test_operation_catalog.py::test_capability_contract_group_recommendation_is_not_spelling`:
  load the example, assert zero spelling/admission/runtime_policy rows, no
  resolver, false job-submittable flags, and unchanged two-member tuple.
- `tests/test_operation_catalog.py::test_capability_contract_unknown_family_refused`:
  parameterize icon and hidden_by unknown targets; valid forward reference first
  loads, replacing it with a missing family fails a named semantic rule.
- `tests/test_operation_catalog.py::test_capability_contract_conflicting_group_refused`:
  group keys are unique across all catalog_group rows, with or without display.
  Each key names one ordered member set; display identity is that same key.
  A repeated key with different members or display payload must fail;
  identical duplicate remains rejected by existing duplicate-row rule.
- `tests/test_operation_catalog.py::test_capability_contract_untyped_execution_field_refused`:
  inject execute/serves at relation, display and recommendation levels; all fail.
- `tests/test_operation_catalog.py::test_capability_contract_ambiguous_spelling_refused`:
  two display spellings sharing word/context but naming different operations fail;
  group-id target and list-valued operation also fail; a confirmed single target
  remains loadable without admission.
- `tests/test_operation_catalog.py::test_capability_contract_order_and_nested_freeze`:
  load two display families in deliberately nonalphabetical order, with reversed
  operation order and ordered suppression refs; assert exact relation/member/
  suppression tuples and map insertion order. Attempts to mutate display,
  recommendation, operations and hidden_by fail. Assert boolean/rank types and
  duplicate rank refusal without increasing any allowed fixture count.

Existing bounded synthetic regression nodes for a maintainer to run on this
documentation-only patch (not executed here):
`tests/test_operation_catalog.py::test_valid_catalog_loads_and_keeps_relation_order`,
`tests/test_operation_catalog.py::test_deeply_frozen`,
`tests/test_operation_catalog.py::test_catalog_group_unknown_operation_refused`,
`tests/test_operation_catalog.py::test_duplicate_spelling_in_context_refused`,
`tests/test_operation_catalog.py::test_relation_cannot_grant_execution`.
No shipped-catalog count test, generator or budget refresh is requested here.

Expect it to work because: a closed presentation payload plus explicit semantic
family validation would preserve the existing separate reader shapes, while
exact operation facts remain the only input to spelling/admission/policy
relations. The image/video/audio evidence above disproves automatic conversion.
The current deliverable only records this safe boundary; functionality is
unverified and no tests ran. Confidence: 0.95 in the schema-only blocker;
0.75 in the proposed contract pending the ownership decision and executable
synthetic tests. It does not establish shipped-policy equivalence.

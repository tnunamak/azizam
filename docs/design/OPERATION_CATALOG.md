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

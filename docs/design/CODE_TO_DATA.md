# Code to data

Status: plan, revision 2, 2026-11-05. Revision 1 was red-teamed by an independent reviewer. This revision takes its
findings: the rule, the oracle, the slice order and four slice designs changed. Slice S1 is on this branch.

Inventory of every site: an internal research note.
This plan is the workflow half of the same goal as [ENGINE_CONTRACT.md](ENGINE_CONTRACT.md), which already makes
engines data through a bounded, versioned `http-v1` grammar and reviewed adapters. It reuses that grammar and its
proof method (§10 there: behaviour evidence first, the ratchet falls only after).

## Nouns

A **workflow** is a typed manifest that maps portable inputs to an engine binding (CORE_CONTRACTS.md). An
**offering** is what one engine can run (ENGINE_CONTRACT.md). Both are listed and run; neither is the other.

## The rule

Core must not select behaviour by a particular workflow, model or engine identity, or by an intent or modality
spelling. Core interprets validated contracts. Reviewed protocol frontends and adapters, and real algorithms, may know
the vocabulary their wire format or semantics require: the OpenAI images frontend knows `/v1/images/generations`
and its fields; a MIME parser knows image formats; ComfyUI's adapter knows its history format.

Within the contracts, a new instance or composition is data only: a new workflow, a new engine of an existing
protocol, a new kind of job built from existing primitives. A new transport, primitive, effect or lifecycle needs a
reviewed extension (an adapter), as in ENGINE_CONTRACT.md §4.

Data extensibility is not authority extensibility. A declared label (capability, `trusted`, resource class) never
grants permission. Owner data selects reviewed primitives and policy profiles; it cannot redefine their enforcement.
A missing authorization or resource declaration makes a workflow "needs configuration", never free to run.

## Why (what the inventory found)

A new kind of job cannot be added as data today:

1. **No generic execution contract.** Each modality has its own route. The one run-by-id route accepts only
   `video.edit`. The runner (`jobs.run`) and the graph compiler return media assets from fixed image, video and audio
   keys; a JSON result, several named files or an audio input have no path.
2. **Many hand-kept intent tables** decide capability, protocol, resource class, OpenAPI and catalog membership
   (capability_ids, model_metadata, capabilities, models_catalog, local_capabilities, me_media; and five in proxy.py).
   An intent missing from them, such as `smell.infer`, gets no metadata: `model_metadata.from_intent` returns None.
3. **Capability is inferred from the intent prefix** when a manifest leaves it out, and some do (the LTX video
   manifests).
4. **Copies of "standard" and "route-managed" input-name sets** per modality.
5. **Engine graphs in Python** (video extend, MiniMax extend, subject select, identity scene) and `_meta` injection
   with input-name guessing in proxy.py.
6. **A loosely checked manifest schema.** The validator checks field presence, not every input spec or output; it
   does not accept object inputs that OpenAPI advertises; the mesh validator is separate and stricter.
7. **Frozen at startup and unversioned.** The workflow registry and default models change only on restart. A stored
   manifest cannot carry its graph. A manifest hash does not cover a separately referenced graph file. A bundled
   manifest silently wins an id clash.

## Done means

1. **The unfamiliar-workflow acceptance matrix passes**, through the real owner and member APIs, with names generated
   after process start and no code edit:
   1. Register an unfamiliar workflow and binding built from supported primitives. Without restart it appears in the
      policy-filtered catalog, the generic OpenAPI, `/v1/models` where representable, and a generated form.
   2. Run one structured JSON result and one non-image asset or mixed result through the public route. Check the
      effective inputs, output count and types, authenticated delivery and exactly one receipt.
   3. A denied App, a denied resource claim, an invalid input and an invalid output each cause zero upstream effects.
   4. Update, disable and delete while queued and while running; restart and resume pinned work; roll back a failed
      update.
   5. Repeated idempotency keys, generated defaults (seeds), a lost submit reply, cancel and fetch races, private
      assets.
   6. A second workflow with the same intent but a different capability and I/O, plus a non-workflow offering: no
      global intent overwrite.
   7. Positive controls: put back an identity branch or a free-admission default and prove the checks fail.
2. **Behaviour preserved.** Each moved route keeps its frozen wire goldens (requests, errors, failure paths) and one
   real job per route succeeds on the live gateway.
3. **The literal ratchet** (`tests/test_domain_literal_ratchet.py`, `[ratchets] domain_literals` in
   tests/budgets.toml, compared with the target branch by the gate) is a review signal. It started at 836. It falls as
   slices land, ending at reviewed protocol-edge sites only, each listed with its reason. It is not the oracle: a count
   can be gamed by moving branches into a table read by a hard-coded key.
4. **Independent review** passes each slice.

Progress is reported as "N of 8 slices".

## Slices (revised order)

| # | Slice | Design |
|---|---|---|
| S1 | Noun and ratchet | Done on this branch: `compile_workflow`; ratchet at 836 in `[ratchets]`. |
| S2 | Execution contract | A versioned **execution envelope** resolved once per request from one registry snapshot: workflow id and revision, graph digest, selected binding and engine/offering revision, effective normalized inputs (generated defaults such as seeds resolved once, after idempotency, and persisted), effect and resource profile, trust and exposure. **Typed named results**: JSON value, asset, list; output selectors, required and count checks, bounded fetch, delivery ownership and privacy. **Media inputs** resolve to authorized assets with type and size checked and origin and path rules applied. `compile_workflow` stays the graph adapter's compiler, not the universal one; synchronous and asynchronous adapters satisfy one core lifecycle. An unsupported form fails at save, never on a member request. |
| S3 | Schema and validator | A closed, versioned structural schema and semantic validator: execution-affecting fields closed; namespaced annotations where safe; objects, arrays, media references, nested constraints, output types, binding completeness, precise errors. Adapter descriptors expose roles for already-reviewed implementations; an unknown binding or role never executes. The mesh validator's rules move into this schema: mesh is an ordinary workflow, not a separate contract. Generated defaults use a tagged generator (`{generate: random_integer}`), not a magic string. Legacy audio aliases and the 30 s default stay in the audio frontend's compatibility profile. Missing capability is backfilled explicitly with recorded provenance. |
| S4 | Live, versioned registry | Atomic snapshots plus **pinned execution revisions**: a queued job keeps its envelope; revocation is still checked before effects. Owner edits use compare-and-swap revisions. Defined behaviour for update, disable and delete while queued or running, restart with old envelopes, rollback, cache invalidation (OpenAPI included). A stored workflow may carry its graph inline with structural bounds (nodes, depth, bytes); publishing a graph to Apps is a separate owner decision from saving it. Id, alias and OpenAPI-name clashes are refused at save. Includes the minimal owner API and form. |
| S5 | Registry projections | One immutable effective registry over workflows and engine offerings, indexed by id and revision. Intent is an attribute and index, not a key for capability, I/O or resources. Catalog, OpenAPI, forms, policy and receipts project the same revision. Compat-API membership (for example the OpenAI images list) is an explicit validated contract match, not inferred from a capability word. OpenAI-style metadata is a lossy edge projection and never gates execution. Replaces the hand-kept intent tables. |
| S6 | Generic run route | `POST /gateway/v1/workflows/{id}/run` for any workflow, on S2 to S5, with the core admission, receipt and job lifecycle. Proven by the acceptance matrix. Prior art: Replicate predictions and versions, fal queues. |
| S7 | Modality routes become frontends; graphs leave Python | Same method as the audio route: goldens first. Image, video and mesh routes become thin frontends over S6. A frontend is a registry row that names a reviewed wire shape (request transform, result shape, synchronous or job); a new route on an existing shape is data, a new shape is code. Image and video leave `_meta`. Chain, subject-select and identity graphs become templates with declared node roles. Mesh: no cross-vendor 3D API standard exists. `/v1/meshes/generate` is replaced (Azizam is unreleased: no alias) by `/v1/3d/generations` in LocalAI's shape (JSON `image` as URL, base64 or data URI; `response_format` `url` or `b64_json`), plus today's raw-GLB multipart shape (Stability SF3D) as a second row, which keeps byte-identical output provable. A Hunyuan3D-2 `/generate` shim (Blender clients) and an asynchronous job frontend come only when a named client needs them. Evidence: `~/code/divan/api-contract-design/no-cross-vendor-3d-generation-api-standard-exists-so-azizams-mesh-route-matches-only-stability-sf3d-and-localai-3d-generations-while-most-clients-speak-hunyuan3d-2-generate-or-async-task-apis.md`. |
| S8 | Defaults, config, owner UI polish | Literal default fallbacks leave with the slice that replaces their owner; per-family config schema; "add workflow" authoring polish. |

Do not make every existing route wait for a general workflow language. Ship the smallest bounded grammar that proves
two meaningfully different workflows and one non-workflow offering, and keep the reviewed-adapter path for the rest.

## What stays code

Protocol frontends and their wire schemas, error vocabularies, security limits, adapter internals, real algorithms.
Exemptions are narrow and reviewed per site, not whole mixed modules.

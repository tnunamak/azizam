# Workflow schema and validator (code-to-data S3)

Status: design, revision 2, 2026-11-07. Base: main at d53c78c38. No production code changes on this branch.
Appendix: [CODE_TO_DATA_S3_workflow.schema.json](CODE_TO_DATA_S3_workflow.schema.json), a draft of the structural
schema.

S3 is row S3 of [CODE_TO_DATA.md](CODE_TO_DATA.md). It extends what S2 built
([CODE_TO_DATA_S2.md](CODE_TO_DATA_S2.md)). It does not re-decide S2. Where this design touches an S2 decision, it
names the decision and says what it adds.

Prior art: the divan entry "Runnable-unit schemas (Cog, fal, ComfyUI, CWL, Kubernetes CRDs, JSON Schema/OpenAPI, GitHub
Actions, MLflow, KServe OIP, Hugging Face tasks) agree on separate version layers and on binding kept apart from data
shape, but differ on unknown fields, defaults, output enforcement, error aggregation and migration" (2026-11-07). Source
labels in this document (KU1, CW2, FA7 and so on) are that entry's labels. The code sites come from the divan entry
"Azizam code that should be data: inventory of proxy.py and gateway/ (2026-11-05)", including its report 3 (the plan
red team, finding M3). No new web research was done for this design.

## Problem

The manifest validator is loose. These facts are from the code at the base commit.

1. **`validate_manifest` checks presence, not shape** (`gateway/manifests.py`). It does not check the value of
   `schema_version`. It skips an input spec that is not a mapping. It does not check that a default fits its spec,
   except for non-finite numbers. It accepts any unknown key at any level. A typo such as `maxium: 9` is silently
   ignored, so the bound the owner meant does not apply.
2. **Two validators.** `gateway/mesh_manifest.py` is a second, closed, mesh-only validator with its own input types
   (`image`, `object`), its own hash and its own dataclass. The mesh route (`gateway/mesh_api.py`) uses only it.
3. **Authority fields in the document.** The image route builds its policy request from `exposure`, `trust_tier`,
   `resource_tier` and `tags`, read from the manifest with defaults (`_image_manifest_policy_metadata` in `proxy.py`).
   `trusted` decides the stable or experimental tier and the unreviewed caveat (`gateway/capabilities.py`). Because
   the validator accepts any key, an extension package manifest (`gateway/extension_packages.py`) can set them.
4. **Generated values are code keyed by a name.** A random seed is drawn when the caller gives none, in six
   places, with eight draws: `audio_graph._draw_seed`, `_reseed_image_workflow` (two draws), two video request paths in
   `proxy.py` (three draws), the seam pass of the tile composition, and `a1111_api`. Each finds the input by the literal name `seed`.
   Only the audio draw respects the manifest's bounds.
   S2 D1 already reserves the envelope's `generated` block for this; it is always empty today.
5. **Bindings are not closed.** A binding key resolves to an adapter descriptor only for the graph engine (`comfyui`,
   through `registry/protocols/node-graph.yml`). The `gateway` binding selects a reviewed composition by the string
   `compose` and `proxy.py` branches on its value. The mesh binding key is a service id (`trellis`). Two image edit
   manifests map an input, `seed`, that they do not declare.
6. **Capability is inferred.** 33 of 38 bundled manifests have no `capability`. `workflows_catalog._capability_from_intent`
   infers it from the intent prefix, against a closed list of eight words.
7. **No object, array or nested types** in the shared validator, while the OpenAPI projection already advertises
   `object` (`gateway/workflow_openapi.py`). Media inputs declare no accepted types: the routes pass the constant
   `("image/*",)` to the S2.6 resolver.
8. **Outputs are half checked.** S2 checks result kind and media family (`output_selectors.wrong_type_reason`). Nothing
   checks result count against the contract, or a JSON result's shape.
9. **First error only.** Each check raises on the first problem. An owner who saves a document with five problems
   learns about one.

## Decisions

### D1. Two layers: a structural schema and a semantic validator

- **Structural layer.** A JSON Schema 2020-12 document, `manifests/workflow.schema.json`, `$id: azizam/workflow/2`. It
  closes every level of the workflow document, except the interior of a binding. It checks types, names, sizes and
  required fields. The appendix is the draft.
- **Semantic layer.** One Python module, `gateway/workflow_contract.py`. It checks what a JSON Schema cannot say
  without a reference to other data: a default fits its spec, a map names declared inputs, a generator is registered,
  a binding fits its adapter descriptor, an accepted media type is known to the reviewed sniffer.
- **One entry point.** `check_contract(doc, context) -> Report`. The context says which mode runs: `load` (a bundled or
  stored document), `save` (an owner or package write; S4 calls this mode), or `pin` (a pinned contract, checked against
  its pinned descriptors, as S2.5a does now). `validate_manifest` and S2.4's `check_for_save` become stages inside it.
  The stored-record path (`service_registry.normalize_workflow_record`) and the package path
  (`extension_packages._normalize_workflow`) call the same function.

Grounding: this is the repo's own pattern for protocol descriptions (`registry/protocols/protocol.schema.json`, a closed
JSON Schema, then a semantic compiler in `gateway/engine_protocol/description.py`). The dialect is declared, as JSON
Schema requires (JS1). Kubernetes also uses a restricted structural schema plus separate semantic rules (KU1).
Departure from KU1: our schema uses internal `$ref`; Kubernetes forbids it. The schema is ours and fixed per version,
so references cannot point outside it.

### D2. Closure: refuse unknown fields; annotations are namespaced and never execute

- **Refuse, do not prune.** An unknown key at any level is an error with its path. This includes the interior of a
  binding, which the adapter descriptor closes (D7).
- **Why refuse.** A save is an owner action with a person or an agent waiting for the answer. A pruned typo (`maxium`)
  changes what runs without telling anyone. A refusal with a path costs one edit.
- **Annotations.** Free data may live only in an `annotations` map at three places: the document root, an input spec
  and an output spec. Keys are `<domain>/<name>` (for example `example.org/review-note`). At most 32 keys per map; the
  total size of all annotations is bounded (8 KiB per document, a deployment limit). Values are any JSON value.
- **Annotations never execute.** Core never reads an annotation. A test reads the gateway's sources and fails if code
  outside the projection layer reads the `annotations` key. Annotations are not allowed inside a binding, so they
  cannot carry execution settings. They are excluded from the contract digest (S2 D1) and from the manifest hash, as
  `prompt_hint` already is.
- **Authority does not live in the document.** The v2 schema has no `trusted`, `source`, `exposure`, `trust_tier`,
  `resource_tier` or `tags`. `trusted` and `source` move to the registry record's provenance, which the loader sets
  from where the record came from. The values for every bundled record do not change (all 38 are `trusted: true`,
  `source: local`). The image route stops reading the four policy fields from the manifest.
- **Policy facts are record data, and absence fails closed.** `exposure`, `trust_tier`, `resource_tier` and `tags`
  become record-level policy facts of the registry record, beside provenance, never in the workflow document. Release
  values live in a reviewed release file (`registry/workflow_policy.yml`, keyed by workflow id); owner changes are
  registry patches, as for any record. The migration writes each bundled workflow's current effective values there
  (today all are the reader's defaults: `default`, `stable`, `batch`, no tags), so behaviour does not change. After
  S3.5 the reader has no defaults: a workflow without its policy facts is "needs configuration" and runs nothing. This
  is the plan's rule that a missing authorization or resource declaration never means free admission. S5 owns the
  meaning of the values; S3 only moves them and removes the defaults.

Grounding: CWL strict validation refuses unknown fields that are not namespaced (CW1). That is the precedent we follow.
We depart from Kubernetes, which prunes (KU1), from GitHub Actions, which warns (GA2), from MLflow, which ignores
top-level extras (ML1), and from Pydantic's default `ignore` (FA2). The split between what may execute and what is only
a note follows CWL requirements and hints (CW2). The `<domain>/<name>` key form comes from Kubernetes object
annotations, which the divan entry does not cover: ungrounded in the corpus. Moving authority out of the document is
the plan's own rule ("Data extensibility is not authority extensibility"); no surveyed system was checked for it.

### D3. Versions: three layers; migration by storage rewrite, with no old-version reader

- **Three version layers**, as separate fields:
  1. `schema_version`: the document dialect. S3 makes it `2`. The validator accepts exactly the versions it knows.
  2. `version`: the author's version of this workflow. Free text, shown to people.
  3. The registry revision (S4) and the contract digest (S2 D1): what a job pins.
- **Migration.** One storage version, v2. A pure converter, `convert_1_to_2(doc) -> (doc, notes)`, runs in two places:
  - a repository rewrite of the 38 bundled files (S3.5), reviewed as a diff;
  - a one-time migration of stored owner records in the admin database, with a dry run, in the form of the config-homes
    registry migration (`gateway/registry_migration.py`: a pure plan, a report with ids and field names only, then an
    apply). A stored record is either a whole document or a patch over a release record (registry layering). A patch
    is converted by applying it to the v1 release record, converting the result, and storing the difference from the
    v2 release record. This is the same method as config-homes 8.3.
- **No v1 reader after the migration.** The validator refuses `schema_version: 1` with the code
  `schema_version_unsupported` and a pointer to the converter. A job pinned with a v1 contract fails with
  `definition_unavailable`. This is the S2.2 precedent: Azizam is unreleased, so there is no reader for an old shape.
  Deploy S3.5 when the job queue is idle; `safe-restart` already waits for idle.
- **A document that cannot convert** is stored disabled with its error list, and shown as "needs configuration". It
  never runs. The migration never drops it.
- **Packages.** An extension package with a v1 workflow is refused at install with the same code. The converter is a
  command (`scripts/workflow_contract.py convert`), so a package author can convert offline.

Grounding: layered version identity recurs in at least five systems (JS1, OA1, CO3, CO4, KU2, FA3, ML4). Kubernetes is
the only surveyed conversion precedent (KU2): one storage version, a conversion between versions, and a rewrite of
stored objects ("write them with the same content"). We follow the storage rewrite. We depart from its served old
versions and conversion webhook: we serve one version only, because the product is unreleased and S2.2 already chose
no compatibility reader.

### D4. Types: closed, nested, bounded; media inputs are references with declared accepted types

The input type grammar (v2):

| Type | Keys besides the common ones | Notes |
|---|---|---|
| `string` | `default`, `enum`, `min_length`, `max_length` | No `pattern` in v2 (open question 4). |
| `integer`, `number` | `default`, `enum`, `minimum`, `maximum`, `step`, `offset`, `generate`, `settable` | `step` and `offset` keep today's grid rule. |
| `boolean` | `default` | |
| `asset` | `accepts` (required), `max_bytes` | A media reference. `accepts` lists exact types (`image/png`) or a family (`image/*`). |
| `object` | `properties` or `opaque: true`, `default` | `properties` is closed. `opaque` takes any JSON object inside the deployment's JSON limits (S2.3: depth, items, bytes). |
| `array` | `items` (required), `min_items`, `max_items` (required, at most 256), `default` | `items` is one spec, any type, including `asset`. |

Common keys: `type`, `label`, `help`, `description`, `required`, `annotations`.

Rules:
- **Nesting** is at most 4 levels. An object has at most 64 properties. A document has at most 64 inputs.
- **Defaults are checked.** A default must pass the same check as a caller's value, at every save and after deployment
  narrowing (`apply_deployment_inputs` already re-checks it). Defaults are applied by the gateway, once, before the
  request is bound (today's `compile_workflow` rule). A default is data, never a code fallback.
- **Enums are typed.** Every enum value has the input's type.
- **Media references.** An `asset` value is a reference (asset id, upload, or URL), never bytes in the document. The
  S2.6 resolver (D7 of S2) checks it. S3 changes one thing there: the routes pass the input's `accepts` from the
  contract, not a constant. Every `accepts` entry must be a type the reviewed sniffer can measure, or a family it knows;
  otherwise the save is refused (`accepts_unknown_type`). S2 D6 already refuses an output type the sniffer does not
  know; this is the same rule for inputs.
- **`type: image` is removed.** It was mesh-only. The mesh input becomes
  `asset` with `accepts: [image/png, image/jpeg, image/webp]`, which is what `mesh_manifest.sniff_image` accepts today.
- **Caller-claimed media type is ignored**, as in S2.6.
- **Media-bearing inputs are found recursively.** An input is media-bearing when its spec is `asset`, or an `array` or
  `object` whose spec contains an `asset` at any depth. Every rule that today asks "is this input media" (S2.4's
  source-echo check in `output_selectors.check_echo`, which tests only `spec.type in MEDIA_INPUT_TYPES`; the resolver;
  OpenAPI) uses one core function, `media_bearing(spec)`. Without it, an array of assets mapped to a node with an
  incoming link could be selected as a result and hand a source back, which S2 D5 forbids.
- **A required input that the binding never applies is refused at save** (`input_unbound`), when the adapter descriptor
  says inputs reach the engine only through its map (`inputs_via: map`, D7). An optional unmapped input stays legal;
  supplying it at run is refused, as `compile_workflow` and the mesh route already do.

Grounding: records, arrays, enums and typed File inputs follow CWL (CW1, CW2). Primitive types and constraints follow
JSON Schema (JS1, JS2). A bounded, restricted subset follows Kubernetes structural schemas (KU1). Media references
validated outside the schema follow pattern 5 of the entry: JSON Schema content annotations are not validated
(JS2 "MUST NOT automatically decode"), and Cog, fal and Hugging Face move files as references (CO5, FA5, HF4). Opaque
objects are allowed only when declared and bounded, because Cog's opaque dictionaries bypass type validation (CO1).
Checked defaults go further than JSON Schema, which only recommends a valid default (JS2); Kubernetes applies defaults
on the server (KU1), as we do.

Why not use JSON Schema itself for input specs: JSON Schema is open by default, treats `default` as an annotation and
has keywords we must not run (`$ref` to outside, `pattern` on member input, `oneOf`). We would need a restricted profile
plus `x-` keywords for `asset`, `generate` and `step`, which is a second grammar in disguise. Instead, the input grammar
is ours, and the OpenAPI projection (`workflow_openapi._input_schema`) maps it to JSON Schema, as it does today.

### D5. Outputs: declared type, count and JSON shape, checked after selection

An output spec (v2): `media_type` (required), `count: {min, max}`, `schema` (only for `application/json`), `label`,
`help`, `description`, `annotations`.

- **Kind** comes from the media type: `application/json` is a `json` result; any other type is an `assets` result. There
  is no separate `kind` field, so the two cannot disagree.
- **Count.** Absent `count` means `{min: 1, max: <deployment max_assets>}`. That is today's behaviour: zero files is
  already `result_missing`, and the asset limit is S2.3's. The validator writes the filled `count` into the normalized
  contract, so the pinned contract states it.
- **Count must be possible at save.** Each adapter gives a reviewed hook, `selector_cardinality(selector) -> (min, max)`
  (for the graph adapter, `take: first` and `take: single` give at most one item). Core checks only that the declared
  `count` interval meets the selector's interval; if they do not meet, the save is refused (`count_unreachable`).
  Core does not know why a selector yields what it yields. A workflow that declares `count: {min: 2}` on a `take:
  single` selector is refused at save, not after a run.
- **JSON shape.** `schema` uses the D4 input grammar (without `asset`). A JSON result is checked against it after the
  adapter selects it.
- **Where.** The adapter's `collect` and `results` call one core function, `check_result(spec, kind, items)`, beside
  `wrong_type_reason`. A refusal is `SelectionRefused` with `result_count` or `result_schema`. It publishes nothing,
  under S2 D4 and S2.5a.

What S3 adds to S2: S2 D5 checks that each output has a selector and that selectors do not echo a source. S2.5a checks
kind and media family. S3 adds the count bounds, the JSON value check and the required `media_type`. S3 keeps S2's family
rule for files (an `image/png` output accepts any `image/*` file the sniffer names). Exact subtype matching is open
question 5.

Grounding: declared output types follow Cog (CO1, CO2). Checking the captured output follows CWL (CW2), without its
ExpressionTool exemption. We depart from MLflow, which does not validate outputs (ML1), and from GitHub Actions, which
allows undeclared outputs (GA5). The entry's synthesis says that checking outputs against declared types and counts
goes beyond the prior art. Count bounds have no surveyed precedent (CW2, CO1 negative results).

As built (S3.8): `check_count` and `check_result(spec, kind, items)` sit in `gateway/output_selectors.py` beside
`wrong_type_reason`. A JSON result is one item (the decoded document, even an array); the shape is checked with
`workflow_contract.value_problems`; a refusal is `SelectionRefused` (`result_count`, `result_schema`) and holds no value
or answer-supplied name. Refusal order is unchanged: zero files is `result_missing`, two files for `take: single` is
`result_ambiguous`, a wrong media family is `result_wrong_type`, and only then the count.
- Graph adapter: `collect` counts the kept files after `select_outputs` (a JSON result counts as one document there, so two
  JSON files stay `result_ambiguous` in `results`); `results` checks the saved assets and the bounded-decoded JSON value
  before the bundle returns. HTTP adapter: `_bundle` checks each result beside `wrong_type_reason`.
- `fetch_selected` (a route that takes bytes) keeps selection-count semantics: a download that fails is left out as before
  and no delivered-count recheck follows. A test pins this.
- Not covered: the HTTP job-pattern path (`Pending`, then the executor's `fetch_assets`; the HTTP adapter has no `results`)
  and the graph adapter's legacy `fetch_assets` publish with no output spec, so no check runs there.
- No normalization output exists yet (`check_contract` returns a report; the document hash is of the document as written), so
  the absent `count` is not written into the contract. `effective_count(spec, max_assets)` computes `{min: 1, max:
  <deployment max_assets>}` at check time. The per-output maximum never replaces the aggregate `max_assets` guard.
- `selector_cardinality` is a method on each adapter. The graph one is registered under its adapter id in
  `binding_registry`; `manifests.collect_manifest_errors` (so `enforce_contract`, hence loads, owner saves and package
  saves) looks it up through the binding descriptor and refuses `count_unreachable` at `("outputs", name, "count")` after the
  selector normalized. The HTTP hook has no production save path (an http-v1 description has no binding descriptor), so
  `check_for_save(..., cardinality=hook)` takes it explicitly.

### D6. Generated defaults: a tagged, reviewed generator, resolved once

```yaml
seed:
  type: integer
  minimum: 0
  generate: {generator: random_integer, minimum: 0, maximum: 4294967295}
```

- **`generate` is a sibling of `default`, never a value of it.** The two are mutually exclusive. A tagged value inside
  `default` would be ambiguous for an `object` input, whose default can be any object. A string such as `"random"` in
  `default` fails the type check, because a default must fit its spec (D4).
- **Generators are reviewed code**, registered by id, like adapters. S3 registers one, `random_integer`. Each generator
  declares its parameters and how its value persists (`value`, `digest` or `none`, S2 D1). A document names a generator
  by id; an unknown id is refused at save (`generator_unknown`).
- **Range.** `random_integer` draws from the generator's bounds intersected with the input's effective bounds (after
  deployment narrowing). The generator's bounds are required, so a draw is never unbounded. An empty range is refused at
  save and at deployment load (`generator_range_empty`). This reproduces the audio route's rule (`_draw_seed`) and the
  image route's range (0 to 2^32-1) as data.
- **Once.** The value is drawn only when the caller gives none. It is drawn once, after idempotency resolution, before
  the envelope is written, and stored in the envelope's `generated` block with the generator id (S2 D1). A retry,
  restart or replay reads the stored value. A crypto-strength source is used; tests inject one.
- **Gateway-only inputs.** `settable: false` marks an input that a caller cannot set; only its generator fills it. It
  requires `generate`. Two bundled edit manifests use it: they map a `seed` that the route draws, and they do not
  publish it (`flux2-klein-edit`, `flux2-klein-4b-edit`). The OpenAPI projection omits such an input, as today.
- **No core code names `seed`.** S2 D1 already says so; S3 makes it testable.
- **OpenAPI.** A generated input publishes no `default`, as today. Describing the generator in OpenAPI is open question 3.

Grounding: no surveyed system declares a generated default such as a seed as data (N1 to N10; the synthesis). fal's
default factory is the nearest precedent (FA6), but it is code. JSON Schema treats `default` as an annotation only
(JS2). The tagged form is the plan's own decision (row S3) and the red team's (M3). Otherwise ungrounded.

### D7. Bindings: the key names an adapter; a descriptor closes the block; roles name reviewed implementations

- **Resolution.** A binding key must equal the `binding` field of exactly one protocol description. Today only
  `node-graph.yml` has one (`comfyui`). S3 adds two descriptions, both for implementations that exist and are reviewed:
  - `gateway-compose.yml` (`binding: gateway`, `executor: adapter:gateway-compose`): the in-process compositions.
  - `http-multipart.yml` (`binding: http-multipart`, `executor: adapter:http-multipart`): the multipart sender the mesh
    route uses (`gateway/http_multipart.py`).
- **The mesh binding key changes** from `trellis` (a service id) to `http-multipart`, with `service: trellis` inside.
  The service record does not change. A binding names its target engine with `service` only when its descriptor takes
  one.
- **The descriptor closes the binding.** A description gains a `binding_fields` block, next to S2.4's
  `result_selectors`. It lists each field the binding may hold and its kind. The kinds are a small fixed set that core
  checks with no adapter knowledge:

  | Kind | Core check |
  |---|---|
  | `template` | A template reference (a path today; inline in S4). The template must load and fit its adapter's structural bounds. |
  | `input_map` | Keys are declared inputs. Each target is checked by the adapter's reviewed `check_targets` hook. |
  | `input_list` | Every item is a declared input. |
  | `target`, `target_list` | One engine-side reference, or a bounded list of them (for example graph node ids in `image_nodes`, `prompt_node`, `seed_node`). Checked by the adapter's reviewed `check_targets` hook against the template; core checks only the type and the bound. |
  | `relative_path` | A canonical relative URL path: no scheme, host, query, `..`, empty or `.` segment (the mesh transport check today). |
  | `selectors` | S2.4's selector check, unchanged. |
  | `requirements` | The node-graph `requires` grammar (`workflow_requirements.py`), unchanged. It moves into the binding. |
  | `service` | Names an engine record in the effective registry. |
  | `role` | One of the descriptor's `roles`. |
  | `text`, `enum`, `boolean` | As in the S2.4 selector grammar. |

  The descriptor also states `inputs_via: map | role`: whether inputs reach the engine only through the map (D4's
  `input_unbound` rule applies) or the role's implementation reads them.
- **Roles.** A descriptor may list `roles`. Each role names one reviewed implementation and may add its own fields.
  `gateway-compose.yml` lists `identity_scene`, `offset_inpaint_tile` and `face_likeness`, with the fields each one reads
  today (`image_nodes`, `prompt_node`, `seed_node`, `base_model`, `inpaint_model`, `route`, `model_id`, `workflow`,
  `map`). The binding's `compose` field becomes a `role` kind field.
- **An unknown binding or role never executes.**
  - At save: an unknown binding key, an unknown field, or a role the descriptor does not list is refused
    (`binding_unknown`, `binding_field_unknown`, `role_unknown`).
  - At run: a role or adapter that the descriptor lists but that no reviewed code registered is
    `binding_unavailable`. The job fails before any effect, as `adapter_not_registered` does today. The catalog shows
    the workflow as "needs configuration".
  - The `proxy.py` dispatch on `compose` becomes a lookup in the role registry. A test reads the dispatch and fails if
    it has a fallback branch.
- **The mesh transport block is flattened.** Today it is `transport: {kind: http-multipart, path: /generate}`. The
  kind repeats the adapter, so the converter drops it, and `path` becomes a top-level `relative_path` field of the
  `http-multipart` binding. The descriptor closes the binding, so the closure that `mesh_manifest.py` applies to the
  transport block is kept. Binding fields are flat: the kind set has no nested object kind.
- **Route hints stay closed fields for now.** `edge_only_inputs`, `route_bound_inputs` and `mask_to_source_alpha` are
  route hints on node-graph bindings. They become declared `input_list` and `boolean` fields of the node-graph
  descriptor. S7 moves them to frontend profiles.
- **The two edit manifests that map an undeclared `seed`** declare it as a `settable: false` generated input (D6). After
  that, every map key is a declared input.

Grounding: the binding kept beside, not inside, the input and output contract is pattern 2 of the entry (CO3, CF3,
CF6, GA1, GA3, ML3). Refusing an unknown implementation follows ComfyUI's `missing_node_type` (CF5), GitHub Actions'
unsupported `using` (GA3) and fal's stop on a nonexistent endpoint (FA9). The run-time registration check follows
Kubernetes: schema acceptance is not proof that a controller is installed (KU3). The fixed kind set reuses S2.4's
descriptor grammar.

### D8. Errors: aggregate every error, each with a path and a code

- Each error is `{path, pointer, code, message}`:
  - `path`: a list of keys and indexes, as Pydantic's `loc` (FA7) and fal's queue errors (FA8).
  - `pointer`: the same place as a JSON Pointer, for display (JS1 `instanceLocation`).
  - `code`: a stable word (`unknown_field`, `type_mismatch`, `default_invalid`, `generator_unknown`, `role_unknown`, and
    S2.4's selector codes).
  - `message`: one short sentence. It never holds a caller's value. A document error may name the owner's own keys.
- **All errors are collected**, structural first, then semantic, then binding, sorted by path. The list is capped at
  100, with a final `too_many_errors` entry. A semantic check that depends on a broken part is skipped, so one root
  cause does not produce a cascade.
- **Compatibility.** `ContractErrors` subclasses `ManifestError`. It keeps a separate `primary` error: the error
  today's validator would raise first, chosen in today's check order (for example, a bad input before a bad binding,
  as `validate_manifest` checks them). Its `str()` is the primary's message, so existing callers and their tests see
  the same message. Sorting by path applies to the detailed list only. The member-request input check also collects every error, but each
  frontend keeps reporting the first one, in today's order, with today's `param`. The wire does not change. The generic
  run route (S6) returns the list.

Grounding: Pydantic aggregates all errors with locations (FA7), as fal uses it (FA6, FA8). JSON Schema's verbose output
returns all results with instance locations (JS1). The cap follows GitHub Actions' error-collection limit (GA3).
ComfyUI's mix of early failure and per-node errors (CF5) is what we avoid. The repo already sorts schema errors by path
(`description.py`, `config_layers.py`).

### D9. Mesh is an ordinary workflow

`gateway/mesh_manifest.py` loses its validator, its hash and its `MeshManifest` dataclass. The mesh route reads its
workflow record from the registry, like any route. Each current mesh check goes to one of four places:

| Current check (`mesh_manifest.py`) | Goes to | Why |
|---|---|---|
| Closed top-level, input, binding and transport keys | Schema (D2) | Closure is generic now. |
| `schema_version` must be 1 | Schema (D3) | Generic version check. |
| `id` and `version` non-empty text | Schema | Generic. |
| Input name pattern `[a-z][a-z0-9_]{0,63}` | Schema, for every workflow | All 38 bundled input names already fit (checked). Form-field and OpenAPI safe. |
| Input type in {image, integer, string, object} | Schema (D4) | The generic grammar is a superset. |
| `image` must declare `modality: image` | Removed | `image` becomes `asset` with `accepts` (D4). |
| Integer bounds and defaults are integers; enums non-empty and typed | Semantic validator (D4) | Generic now. Today the shared validator does not check these. |
| A default must satisfy the input's rules | Semantic validator (D4) | Generic now. |
| Map keys are declared inputs | Semantic validator, `input_map` kind (D7) | Generic. |
| A required image input must be mapped | Semantic validator, `input_unbound` (D4) | Generic: any required input. |
| An object input cannot map to a form field; form field name pattern; a target used twice | Adapter `http-multipart`, `check_targets` hook (D7) | These are rules of the multipart wire, not of workflows. |
| Transport kind is `http-multipart`; path is a canonical relative path | Adapter descriptor `http-multipart` (D7): the key names the kind; `path` is a `relative_path` field | Transport grammar. The block is flattened (D7). |
| Binding key is a service id | Removed | The key names the adapter; `service` names the engine (D7). |
| Intent must be `mesh.generate` | Mesh frontend | The frontend row already names the intent and workflow (`mesh_api`, the frontend check near line 242). A workflow does not pick its route. |
| Exactly one required image input; exactly one binding | Mesh frontend profile | The `/v1/meshes/generate` wire carries one file part and calls one engine. |
| Exactly one output, `model/gltf-binary` | Mesh frontend profile | The route answers with one GLB body. |

**The mesh frontend profile** is a check the frontend runs on the workflow it serves, at load and at save. It uses the
validated contract only (input types, `accepts`, output media types, binding adapter). It stays code in
`mesh_api.py` until S7 turns frontends into registry rows. It names no engine.

**What stays code for mesh:** the multipart form decoding (an integer read from text, an object read from JSON text),
the wire error codes (`unknown_field`, `missing_image`, `unsupported_input`, `invalid_input`, `image_too_large`,
`unsupported_image`), the scalar encoding of form fields (`_canonical_scalar`), the exclusive-pool lease and the
multipart sender. The PNG, JPEG and WebP sniff moves to the S2.6 resolver, which already knows those types.

### D10. Capability is declared; a missing one is backfilled once, with provenance

- v2 requires `capability`. Its syntax is a token (`[a-z][a-z0-9_-]{0,31}`). The validator does not check it against a
  closed list. Whether a capability is known and permitted is policy (S5), and an unknown one must fail closed there
  (open question 2).
- The converter backfills a missing `capability` with today's exact rule (`_capability_from_intent`), so the projected
  value does not change. It records the backfill in the document as an annotation,
  `azizam.dev/backfill: {capability: "intent-prefix rule, schema 1 to 2"}`, and in the migration report. The scratch run
  backfills 21 `image` and 12 `video`.
- After the migration, `_capability_from_intent` is deleted. The catalog reads the declared field only. A document
  without `capability` is refused at save (`required_missing`).
- The inference runs once, at conversion. It never runs at load or at request time again.

Grounding: none in the prior art. This is the plan's rule (row S3) and the red team's correction (inventory entry,
report 3, section 5).

### D11. The audio frontend keeps its compatibility profile; the schema does not learn it

- These stay in `gateway/engine_protocol/audio_graph.py`, the audio route's reviewed edge: the text aliases and their
  precedence (`prompt`, then `input`, then `tags`), the text routing rule (`_place_text`), the `type` taxonomy and its
  `music` default, the `native` response format rule, and the 30 s default duration.
- The 30 s default stays a frontend narrowing in `effective_manifest`: it applies only on the audio route, only when
  neither the deployment nor the manifest declares a default. The schema has no aliases and no route defaults. The
  generic compile path and the S6 route never apply them. A test runs a workflow that declares `duration` with no
  default through the generic compile path and proves that `duration` stays unset (and is refused if required).
- The audio route's seed draw leaves `audio_graph.py` for the D6 generator. The audio manifests declare it.

Grounding: the inventory red team (M3) said aliases and the 30 s default belong to a frontend compatibility profile,
and that a plain alias list is not enough without collision and precedence rules. Ungrounded in the external survey.

### D12. Names

- The document's version field is `schema_version`. The module is `gateway/workflow_contract.py`. The structural schema
  is `manifests/workflow.schema.json`.
- `check_contract` is the one entry point. `validate_manifest` stays as a thin caller, so S2 call sites do not change.
- The converter and the effective-registry diff are one command, `scripts/workflow_contract.py`, with subcommands
  `check`, `convert` and `diff-effective`. Agents and owners use the same command.

## What stays code

- **Reviewed adapters and their internals:** graph patching, history parsing, the multipart sender, the http-v1
  executor. A new adapter, a new binding field kind or a new target grammar is a reviewed code change (ENGINE_CONTRACT.md
  §4 and §7).
- **Reviewed role implementations:** the identity scene, the offset inpaint tile and the face likeness compositions.
  Their algorithms stay code; the descriptor only names them.
- **Reviewed generators:** `random_integer` and any later one. A document can only select one.
- **The reviewed sniffer** and its table of measurable media types.
- **Frontend wire shapes:** the audio compatibility profile (D11), the mesh frontend profile (D9), the image routes'
  managed field sets until S7.
- **Security and deployment limits:** JSON depth and size, asset counts and bytes, annotation size, nesting depth,
  error cap. They are trusted deployment config, never document values (S2 D6).
- **The two schema layers themselves.** The type grammar is closed per `schema_version`. A new type is a new schema
  version.

## Data-first check

Each item below must be data, with no code change. A test proves each one with names made at random after start.

- A new workflow on the graph engine with object, array, enum and asset inputs, a JSON output with a `schema`, and a
  count-bounded file output.
- A new workflow bound to a new engine of an existing protocol (an http-v1 description and an engine record).
- A second workflow on the multipart adapter, with a different service and different form fields.
- A new capability token (`smell`), a new intent and a new media type family that the sniffer knows.

The schema and validator name no model, engine, workflow, intent or modality. A token test reads
`gateway/workflow_contract.py` and the schema file and fails on any such word (the S2.5a test pattern).

## Migration of bundled and stored records

### Bundled workflows

The converter rewrites the 38 files in `manifests/workflows/`. A scratch run of a draft converter against the draft
schema in the appendix gave 38 files, 0 structural or semantic errors. The changes, by count:

| Change | Files |
|---|---|
| `schema_version: 1` becomes `2` | 38 |
| `trusted` and `source` leave the document for record provenance (same values) | 38 |
| `capability` backfilled (21 `image`, 12 `video`), with the backfill annotation | 33 |
| `requires` moves into the binding (`comfyui` or `gateway`) | 30 |
| `accepts` added to asset inputs: `image/*` where no type was declared (27), the declared `media_type` otherwise (3) | 30 |
| `generate: random_integer` on `seed` (31 declared and mapped; 2 declared as `settable: false`) | 33 |
| Mesh: `type: image` becomes `asset` with three accepted types; `modality` keys removed; `extensions` becomes `opaque: true`; binding `trellis` becomes `http-multipart` with `service: trellis`; `transport` is flattened to `path` | 1 |
| Policy facts written to the release policy file with today's effective values (not a document change) | 38 records |

The converter's seed rule is keyed by the name `seed`. That is acceptable in a one-time tool, not in core. Its output is
not trusted on its own: S3.6's differential test proves, per workflow and per route, that a generator is declared
exactly where a route draws today (and not on the mesh workflow, whose route sends no seed when none is given).

### Service records and protocol descriptions

- **No service record field changes.** The mesh binding names the `trellis` record through `service`; the record stays
  as it is. The effective-registry check covers the services section and must show it equal.
- **Protocol descriptions change.** `node-graph.yml` gains `binding_fields`. Two descriptions are added
  (`gateway-compose.yml`, `http-multipart.yml`). `protocol.schema.json` gains the `binding_fields` and `roles` blocks.
  The node-graph description's revision digest therefore changes (S2.5a pins it), so a graph job in flight at deploy
  fails its identity check. Deploy at idle (D3).

### Stored owner records

A one-time migration, `workflow-schema-2`, converts stored workflow records and patches (D3). It has a dry run that
reports ids and field names, never values. A record that cannot convert is stored disabled with its errors.

As built (S3.5b): `gateway/workflow_schema_migration.py` (pure plan, report, apply), the dry run
`scripts/workflow_schema_2_report.py COPY_OF_ADMIN_DB`, and the frozen schema 1 release records
`gateway/migration_data/workflow_schema_1_release.json` (the 38 bundled documents at S3.5's base; deleted with the
migration). The step runs inside `ensure_service_registry`, after layering and before the registry is normalized, and is
recorded in the same transaction as its registry write.

- **Whole record** (an owner record, a package's imported record, or an override of a bundled id): converted, with the
  four policy facts written to the record as the old readers served them (the document's value, else `default`,
  `stable` if `trusted` else `experimental`, `batch`, no tags).
- **Patch over a release record**: applied to the frozen schema 1 record, converted, stored as the difference from the
  schema 2 release record. A patch does not commute with the converter (binding rename, `requires` move, seed rule), so
  the frozen copy is needed. Today the release `workflows` collection is empty (the bundled files are a separate layer),
  so a stored patch exists only if a deployer's catalog ships `workflows.yml`; a patch with no known schema 1 release
  record is kept as cannot-convert.
- **Cannot convert**: the record stays as stored, with `enabled: false` and `migration.errors`. A registry load refuses
  a record that fails the contract and leaves it out (the start then shows the registry's left-out notice); it is never served. Its report line is "needs configuration".
- **Package records**: an imported workflow is an ordinary stored record, so it is converted, and the package's keyed
  install proof is refreshed in the same transaction (a record that matched its proof matches the new one; an edited
  record stays detached), so a later package removal still works.
- **Total plan**: each record is planned alone. A failure of the conversion, the contract check or the normalizer marks that
  record cannot-convert (code `conversion_failed`, the exception type only, never its text) and the other records go on.
  A record that is not an object (JSON null included) is kept as stored and listed as needing configuration; a patch whose
  `manifest` is present but not an object is cannot-convert (`manifest_not_object`), as layering would delete or replace the
  release manifest. A failed `$replace` keeps its envelope as the only key; the `enabled: false` flag and the errors go
  inside the replaced document, so it stays a replacement.
- **Concurrency**: the step marker is read before the registry (a marker that says done is followed by a fresh read). A plan
  with nothing to write records the step in a transaction that checks the registry generation is the planned one; on a
  conflict the plan is made again, within the same three attempts. A true no-op bumps no generation and writes no audit entry.
- **Dry run**: reads a private snapshot of the copy and its write-ahead log, so it leaves the file and its folder untouched.

### The equality check

`scripts/workflow_contract.py diff-effective --before <git ref> --after <work tree>` builds the effective registry on
both sides (bundled plus a stored-records fixture, including a patch and a whole owner record) and compares:

1. **Projections:** each workflow's catalog row, OpenAPI component, policy metadata, input and output modalities, and
   requirements result.
2. **Behaviour:** for each workflow with a template, the compiled engine request for a fixed input corpus (declared
   defaults; each numeric bound; each enum value; each optional input absent), byte for byte, with generated values
   fixed by an injected source. For the multipart binding, the form fields sent.
3. **Binding content.** Each binding, canonicalized, must equal the before-binding with the converter's declared edits
   applied, and nothing else. The converter returns its edits as a list of (path, operation) per document; the check
   replays them on the before side. This covers bindings that have no template, such as the three compositions: a
   swap of two map targets in a composition changes no projection and no template request, but it fails here.
4. **Services and protocol descriptions**, record by record.

Every difference must match a named, intended change with a reason. The allowed list is:

- `schema_version`, `trusted`, `source`, `capability`, `requires` location, `accepts`, `generate`, `settable`,
  `annotations`, and the mesh binding key, as in the table above;
- every manifest hash (receipts in internal stores) and every pinned contract and binding digest, but only because
  the document and binding content changed by the edits above (item 3 checks the content itself);
- policy facts move from reader defaults to the release policy file, with the same values (D2);
- the node-graph description revision;
- the mesh workflow's input modalities: its picture input was an uncounted `image` type and becomes an `asset`, so the
  catalog lists an asset input for it. This is the one projection change a client can see. Open question 6.

A difference outside the list fails the check. Comparing compiled requests and binding content, not only
projections, prevents a migration that changes what runs while every listed field looks equal. The check's own
positive control: swap the `steps` and `seam_steps` map targets of the tile composition in the after tree; the check
must fail.

## Build order inside S3

Every step is additive. Each step has the S2 gate:
- the route wire goldens are unchanged: audio, image generate, image edit and variations, video, mesh, identity scene and
  face likeness, including error paths;
- after deploy, one real job per touched route succeeds on the live gateway.

| Step | Content | Default behaviour after the step | Tests and positive controls |
|---|---|---|---|
| S3.1 | The error type (D8): `ContractError`, `ContractErrors(ManifestError)`, path and code on every existing refusal in `manifests.py`, `output_selectors.py`, `workflow_requirements.py` and `mesh_manifest.py`. Collect, do not raise at first. | Unchanged: `str()` is the old first message. | A corpus of bad documents gives the same first message as before, and now a full list with paths. The corpus includes a document with both a bad input and a bad binding, whose first message must stay the input's. Controls: make the collector stop at the first error (the multi-error test fails); choose the primary by sorted path (the two-error test fails). |
| S3.2 | The v2 schema file, `workflow_contract.py` (structural and semantic layers, the D4 type grammar, D5 output checks at save), `convert_1_to_2`, the `scripts/workflow_contract.py` command. Report-only: the loader converts and checks every document and logs the report. | Unchanged: nothing is refused yet. | All 38 bundled files convert with zero errors. Each D4 rule has a refusal test with its path. Controls: put back `maxium`, a `default: "random"` seed, an `exposure` key and a `trusted` key; each is refused at its exact path. Token test on the new module. |
| S3.3 | Binding descriptors (D7): `binding_fields`, `roles` and `inputs_via` in `protocol.schema.json`; `node-graph.yml` extended; `gateway-compose.yml` and `http-multipart.yml` added; the role registry; the `check_targets` hooks; `binding_unavailable` at run. The `compose` dispatch in `proxy.py` reads the role registry. | Unchanged for every bundled binding. | Unknown key, field and role are refused at save. A listed but unregistered role fails `binding_unavailable` with zero engine calls. A pinned job whose role is removed fails `definition_unavailable`. Control: give the compose dispatch a fallback branch; the dispatch test fails. |
| S3.4 | Readers switch (D3, D9): every consumer reads the normalized v2 contract that S3.2's loader makes in memory from the v1 files: the catalog, OpenAPI, capabilities, requirements, the compositions and the mesh route. The mesh route reads its record from the registry and runs the mesh frontend profile; the old mesh validator still runs beside it as a check. The policy reader reads the record-level facts (D2), with the old defaults still filled in. | Unchanged. The files are still v1. | All route goldens, mesh included. A test proves no consumer reads a v1-only field (`trusted`, `source`, `type: image`, top-level `requires`, `transport`). Control: make the mesh route read the old `MeshManifest` again for its image input; the v1-field test fails. |
| S3.5 | Migration and enforcement (D2, D3, D10): rewrite the bundled files; write the release policy file; the stored-record migration with dry run; the loader enforces v2, refuses v1 and drops the in-memory converter; `trusted` and `source` move to provenance; the policy reader loses its defaults; `_capability_from_intent` is deleted. | Enforced. Wire unchanged. Internal goldens move by the named list only. | `diff-effective` passes with only named differences, and fails on the tile swap control. A stored v1 record converts; a broken one is stored disabled and listed. Controls: remove `capability` from a manifest (refused, not inferred). Remove one workflow's policy facts: it shows "needs configuration" and a run makes zero engine calls. Policy reader: a test runs the image route's policy build on a valid v2 contract wrapped in a mapping that records key reads, and fails if any of `exposure`, `trust_tier`, `resource_tier`, `tags`, `trusted` is read from the contract; putting back the manifest read makes it fail. Deploy at idle. |
| S3.6 | Generated defaults (D6): the generator registry, `resolve_generated` after idempotency, the envelope `generated` block, the audio and image routes switch to it. The video draws, the tile seam draw and the A1111 frontend draw stay, listed in the ratchet with their reason. | Same ranges as today. The envelope's `generated` block is filled. | Per route, a differential test shows a draw exactly where one was drawn before. A retry and a restart read the stored value. Erasure clears a non-empty `generated` block (closes the S2 matrix 2.3 gap). Controls: put back `if name == "seed"` in a route (the token test fails); remove the stored value read (the replay test fails). |
| S3.7 | Runtime types (D4): recursive input checking with paths for object, array and asset; `media_bearing` used by the echo check, the resolver and OpenAPI; the routes pass `accepts` from the contract to the resolver; the OpenAPI projection follows the grammar. | Unchanged for bundled workflows: the routes passed `image/*`, and the converted contracts say `image/*`. | Nested refusal paths (for example `inputs.people[2].box`). OpenAPI snapshots unchanged. Controls: pass a constant `accepts` again (a test with a `video/*` input fails); make `media_bearing` look at the top level only (a test with an array of assets mapped to a linked node that is also selected fails to be refused). |
| S3.8 | Output checks (D5): `check_result` for count and JSON shape, called by both adapters; `selector_cardinality` hooks and the `count_unreachable` save check. | Unchanged for bundled workflows: the default count is today's behaviour, and every bundled selector meets it. | S2 matrix item 5 extended: too few, too many, a JSON value that breaks its `schema`. Nothing published after a refusal. Controls: skip `check_result` (the count tests fail); remove the cardinality check (a `count: {min: 2}` on a `take: single` selector is saved, and its test fails). |
| S3.9 | Mesh validator deleted (D9): `mesh_manifest.py` keeps only frontend code (form decoding, wire codes, scalar encoding). | Mesh wire byte-identical, GLB included. | The mesh route goldens. A second mesh-shaped workflow registered as data passes the profile. Every former mesh refusal has a test in its new home. Control: register a mesh-shaped workflow with two image inputs; the profile refuses it. |
| S3.10 | Audio profile boundary (D11): the generic-compile test for `duration`; the audio route keeps its profile. | Unchanged. | Control: apply the 30 s default inside the generic compile; the test fails. |

S3.1 to S3.3 can be built in parallel after the error type is agreed: S3.2 and S3.3 touch different files. S3.4 needs
S3.2 and S3.3. S3.5 needs S3.4: no file changes shape before every reader reads the v2 contract. S3.6 to S3.10 need
S3.5 and are independent of each other.

## Done when

1. Every bundled and stored workflow is v2 and passes `check_contract`. The loader refuses v1.
   Every workflow has its policy facts, and the policy reader has no defaults.
2. `diff-effective` passes with only named changes.
3. The positive controls listed per step each fail when the defect is put back.
4. The data-first check passes with names made after start.
5. The mesh validator is gone, and the mesh route goldens hold.
6. No core module names `seed`. The remaining code draws (video, tile seam, A1111 frontend) are listed with their reason.
7. Independent review of the built steps passes. (Revision 1 of this design had one; see Objections.)

## Open questions

1. **Where the release policy facts live.** D2 puts them in a release file keyed by workflow id. An alternative is a
   `policy` block on the registry record that the loader builds from the release records. **Recommendation:** the
   release file, so the workflow document stays free of authority, and config-homes has one home for each fact.
   Revisit when S5 defines the facts' meaning.
2. **Unknown capability.** S3 checks only the token syntax. **Recommendation:** S5 makes a capability unknown to the
   effective registry "needs configuration", with a positive control. Do not put a closed list back in the validator.
3. **Describe generators in OpenAPI.** **Recommendation:** not in S3, so the OpenAPI snapshots do not move. Add an
   `x-azizam-generated` keyword with S5's projections.
4. **`pattern` on strings and cross-field constraints** (exclusive inputs, one-of-required). No bundled workflow needs
   them. A regular expression on member input is a denial-of-service risk. **Recommendation:** defer. Add them in a
   later schema version when a workflow needs them, with a linear-time engine for patterns.
5. **Exact output subtype matching.** S2 matches the family (`image/*`). **Recommendation:** keep the family rule. Add
   an opt-in exact match only when a workflow needs it.
6. **The mesh input modality change.** **Recommendation:** accept it as the one named visible change. S5 derives input
   modalities from `accepts` for every workflow, which also corrects the video rows.
7. **`requires` placement.** Moving it into the binding is cleaner (pattern 2) but touches 30 files and the
   requirements check. **Recommendation:** move it in S3.5, in the same rewrite, so the document shape changes once.

## Objections

Objections 1 to 15 are a self-critique, written before the independent review. The independent review's findings are
in the table that follows them.

1. **"This is a type system, a binding grammar, a role registry and a generator registry for 38 files. Over-engineered."**
   Each piece replaces code that exists: the loose validator, the mesh validator, the `compose` branch, six seed draws,
   the capability inference. The binding grammar reuses S2.4's descriptor grammar. The design defers what no workflow
   needs: string patterns, cross-field constraints, CEL-like rules, OpenAPI generator keywords and a v1 reader.
2. **"It re-decides S2."** It does not. S2 D1's `generated` block is filled as designed. S2.4's selector grammar and codes
   are unchanged and run as one stage. S2.6's resolver is unchanged; it gets `accepts` from the contract. S2 D8 says S3
   widens the grammar and does not replace it, and the v2 grammar is a superset of v1 apart from the mesh-only `image`
   type and the authority fields. S2.5a's family type rule is kept (open question 5).
3. **"Refusing unknown fields breaks every package and stored record that has one extra key."** That is the intent: an
   extra key today is either a typo that changes nothing silently, or an authority field that should not be there. The
   migration converts and reports each record. A record that cannot convert is disabled and listed, not dropped.
   Annotations give a safe home for notes.
4. **"No v1 reader is reckless: a job in flight at deploy fails."** S2.2 and S2.5a already chose this. The product is
   unreleased. The deploy waits for idle. A Kubernetes-style served v1 would keep a second grammar alive in core, which
   is the problem S3 removes.
5. **"Changing every manifest hash churns every receipt golden."** The hashes are in internal stores. S2.4 moved the same
   paths for the same reason. The client wire does not move. The effective-registry check names the change.
6. **"The converter infers capability from the intent prefix, which is the inference the plan wants gone."** It runs once,
   its output is reviewed as a diff, and it is recorded in an annotation and in the report. After S3.5 nothing infers it.
7. **"Annotations are a smuggling channel."** Core never reads them, a test enforces that, they cannot appear in a
   binding, they are bounded, and they do not change the contract digest. They hold notes, not settings.
8. **"Aggregated errors leak caller values in member answers."** Messages never hold a caller value. Frontends report one
   error, as now. S6 returns the list with codes and paths only.
9. **"`settable: false` is a hidden input, a new kind of authority."** It can only be filled by a reviewed generator. It
   grants nothing. It makes a value that the route already writes visible in the contract, where it was an undeclared map
   key.
10. **"Renaming the mesh binding key moves engine knowledge into the document."** The opposite: the key now names an
    adapter, and the engine is a data reference (`service: trellis`), as a graph binding's engine is chosen by data.
11. **"Moving mesh's wire rules into a frontend profile in code keeps a mesh-only check."** It does, on purpose. The rules
    describe the `/v1/meshes/generate` wire (one file part, one GLB body), which is frontend code by the plan's rule. They
    no longer constrain what a mesh workflow may be; S7 turns the profile into a frontend row.
12. **"The equality check compares projections, which a migration can game."** It also compares compiled engine
    requests and form fields for a fixed input corpus, with generated values fixed. A behaviour change shows up as bytes.
13. **"Keeping the policy-field defaults is a free-admission default."** Accepted (independent review, finding 1).
    Revision 1 kept them until S5. Now the migration writes each bundled workflow's current values as record facts, and
    S3.5 deletes the defaults: absence is "needs configuration" (D2).
14. **"Missing case: a stored owner record that is a patch over a bundled record."** Covered: the patch is converted
    through the release record and stored as a new difference (D3), and the fixture in the equality check includes one.
15. **"Missing case: deployment narrowing that makes a generator range empty."** Covered: the range is checked at
    deployment load, as `apply_deployment_inputs` already checks defaults (D6).

### Independent review

A different model reviewed revision 1 at commit 8ff07ac0e, with the code. It reported eight findings. Each was checked
against the cited code and accepted. Revision 2 answers them:

| # | Finding | Answer |
|---|---|---|
| 1 | D2 kept free-admission policy defaults, against the plan's rule. | Policy facts become record data; S3.5 deletes the defaults (D2; objection 13). |
| 2 | Nested assets escape the source-echo check: `check_echo` marks only inputs whose top-level type is media. | `media_bearing(spec)` is recursive and shared (D4); S3.7 has the control. |
| 3 | The equality check compares requests only for template workflows; a target swap inside a composition is not seen. | The check also compares binding content against the converter's declared edits; the tile swap is its control. |
| 4 | Converting the mesh file in S3.4 broke the mesh route, which still loaded through the v1 validator. | New S3.4 switches every reader to the v2 contract first; the file rewrite moves to S3.5. No reader for v1 stays after S3.5. |
| 5 | The binding kinds could not express graph node id lists or the nested mesh transport block. | Kinds `target`, `target_list` and `relative_path`; the transport block is flattened (D7). |
| 6 | A count that a selector can never yield passes save and fails only after a run. | `selector_cardinality` hook and `count_unreachable` at save (D5). |
| 7 | The S3.4 policy control could not fail: the schema refuses the key before any reader runs. | The control now records key reads on a valid contract (S3.5). |
| 8 | Sorting by path changes which error is first, against the promise that messages stay the same. | A separate `primary` error in today's order (D8); a two-error test and its control (S3.1). |

## Not settled here

- The exact role field lists for the three compositions. They are listed from the current manifests; S3.3 confirms them
  against the code that reads them.
- Whether the video `_meta` seed draws move in S3 or with S7's graph templates. They are listed, not moved.
- The annotation size limit (8 KiB) and the error cap (100) are first values, not measured.

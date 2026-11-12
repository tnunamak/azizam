# Choosing between mesh workflows

## Built

The image-to-3D routes serve every registry record that has the mesh intent and passes the mesh profile. Two ship:
`mesh.generate.trellis-2` and `mesh.generate.pixal3d`. Gateway code names neither.

- **Lookup.** `proxy._mesh_records()` takes the live registry's mesh records plus the bundled ones the live registry lacks,
  sorted by `(rank, id)`. `current_mesh_workflows()` and `current_mesh_workflow(id)` convert them once per record object.
- **Choice (option A).** A request names a record with the optional `model` field on `POST /v1/3d/generations` and
  `/glb`. The field holds a workflow id (or its short form, `pixal3d`). A name that is not a mesh record is a 404.
- **First choice is data.** A request with no `model` runs the record with the lowest `rank:N` tag in its policy facts
  (`registry/workflow_policy.yml`; `workflow_policy.rank_of`). A record with no rank sorts last; a tie goes to the lower id.
  An owner changes the first choice by editing the tags. The shape asks `DEFAULT_WORKFLOWS` for it (`proxy._mesh_default_workflow`,
  the first ranked record the live registry holds), and `choose_workflow(prefer=...)` applies it only when more than one
  record fits the request. It never hides a missing-input refusal.
- **Shipped value.** Pixal3D is `rank:1`, TRELLIS.2 is `rank:2`. This is the owner’s declared default until a
  side-by-side comparison of the same pictures says otherwise. No comparative quality result is claimed here.
- **Availability, facts, probe.** `_mesh_present(id)`, `_mesh_facts` (one catalog row per record) and
  `mesh_api.records_evidence` work per record. A service is available when any of its records is present and its health
  and pool answer. A Pixal3D health failure leaves TRELLIS.2 available.
- **One card.** Both services declare `pool_id: gpu0` with the same observer, so one request at a time holds the card. The
  probe cache is keyed by (health URL, observer URL), so each engine is probed once per listing, each with one observer read.
- **Receipt.** A receipt and a media job name the workflow that ran (`name_receipt(owner, entry.id, ...)`), so two records
  give two names.

Option B (query or header) and option C (an owner setting for the default only) were not built. C is now the rank tag.

## Snapshots that moved

Each through its documented command.

- `tests/fixtures/c2d_parity/{models_catalog,openapi,workflows_catalog}.json`: `tests/fixtures/c2d_parity/capture.py`. Additive.
- `tests/fixtures/headless_openapi.json`: `scripts/run-hermetic-pytest.sh --python-script scripts/generate_headless_fixtures.py`
  (review the `HEADLESS_OPENAPI_JSON` line; see `docs/ops/HEADLESS_ACCESS.md`).
- `tests/fixtures/workflows_v1_converted/converted.json`: `scripts/run-hermetic-pytest.sh --python-script scripts/generate_workflow_converter_fixture.py`.
  Six synthetic schema 1 test examples exercise the converter for the new records. The production migration’s historical
  schema 1 release copy stays unchanged; the new records never shipped in that release.
- `tests/fixtures/workflow_reader_outputs.json.gz`: `scripts/run-hermetic-pytest.sh --python-script scripts/generate_workflow_parity_fixtures.py --readers`.
  Six added records; the existing generator changes only its declared rank tag.
- `tests/fixtures/engine_protocol/legacy-wire.json`: `scripts/run-hermetic-pytest.sh --python-script scripts/generate_workflow_parity_fixtures.py --legacy-wire`.
  Only the two added services enter the seeded projection; existing projections, compiled configurations and request wires are unchanged.
- `tests/fixtures/category_discovery_ungoverned.json`: `scripts/run-hermetic-pytest.sh -s tests/test_route_policy_inventory.py::test_ungoverned_discovery_snapshot`.
  Apply the printed `CATEGORY_SNAPSHOT_DIFF` only for the reviewed workflow listing changes.
- `tests/test_route_graph_freeze.py`: the `added` set names the record, so the stripped document is unchanged.

`tests/fixtures/route_graph_frozen.json` is unchanged from the train base. The freeze test removes the reviewed added
workflow records before it compares the original document.

## Later 3D stages

Texture refine, retopology, UV unwrap and rigging each take a model and return a model. Each is one record with the
intent `mesh.edit`, an `asset` input that accepts `model/gltf-binary`, one `model/gltf-binary` output, and the same
`http-multipart` binding the picture-to-3D records use. They run through the generic run route
(`POST /gateway/v1/workflows/{id}/run`), not the `/v1/3d/generations` rows: the mesh profile needs a picture input, so a
stage never matches those rows (`tests/test_mesh_edit_stage.py`).

Data changes that carry them: the asset sniffer (`workflow_call.sniff`) names a binary glTF by its magic and version, so a
record may accept that type; `registry/capability_words.yml` maps `mesh.edit` to the `mesh` word, so plans grant it;
the frontend selector grammar accepts `match.intent`, so the 3D rows pick `mesh.generate` records by intent and output
type and never pick a stage.

Shipped records (`manifests/workflows/mesh.edit.*.yml`): `retopo`, `uv-unwrap`, `rig-auto`, `rig-humanoid` and
`texture-refine`. All five use the `local3d-stages` service (`engines/local3d_stages_service.py`), one small wrapper
that runs the engine call for the path it is given (headless Blender script, UniRig command line, or a ComfyUI graph for
the texture refine) and answers 503 while its card is busy. The service record shares pool `gpu0` with the generators.

Open: Kimodo text-to-motion (text in, a motion file out, no model input), bake (several output files) and motion
retarget (a BVH motion file input, which the sniffer has no type for). Each needs a contract shape the one-asset,
one-output stage record does not state.

## Calling the stages

Use an App key with mesh access. Send multipart form data to
`POST /gateway/v1/workflows/{workflow_id}/run` with `Prefer: wait=10` (or poll the returned job).
The `mesh` file part is a binary glTF model. The workflow id in the path selects the stage; no `model` form field is needed.

| Workflow id | Extra form fields | Service path |
| --- | --- | --- |
| `mesh.edit.retopo` | `target_faces` (default 8000) | `/retopo` |
| `mesh.edit.uv-unwrap` | none | `/uv` |
| `mesh.edit.rig-auto` | none | `/rig` |
| `mesh.edit.rig-humanoid` | none | `/rigify` |
| `mesh.edit.texture-refine` | `reference` image file; optional `seed`, `resolution`, `steps` | `/refine` |

Read the resulting model through the job's result links. Mesh generation remains
`POST /v1/3d/generations` or `/v1/3d/generations/glb`, with `model=pixal3d` or `model=trellis-2`.

The wrapper services and their engine dependencies must be installed and registered by the owner. The deployment
fixture provides sample service data; bundling a workflow does not install or start a service. Configure the stage
wrapper paths and shared scratch directory described in `engines/local3d_stages_service.py`. ComfyUI must be able to
read that directory. Set `HF_HOME` for the rigging process's installed weights.

Measured stage limits: retopology may return decimated triangles when QuadriFlow fails; humanoid rigging fits by
bounds and does not provide a fitted control rig. Texture refinement was proven on a roughly 16k-face mesh, with
the installed EGL patch. Bake, motion generation and retarget remain outside these five contracts.

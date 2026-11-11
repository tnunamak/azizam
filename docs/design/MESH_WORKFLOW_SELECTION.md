# Choosing between mesh workflows (follow-up, not built)

## Today

The image-to-3D routes serve exactly one workflow record. `proxy.py` reads the id of `mesh.generate.trellis-2.yml` once at
import (`_MESH_WORKFLOW_ID`). `current_mesh_workflow()`, the "record is present" check, the catalog listing and the pool
probe all use that one id. To use another engine, an owner saves a different contract under the same id
(`docs/examples/mesh-pixal3d/`). That swaps engines. It cannot offer two engines at once.

A second bundled mesh record under another id does not work. The 3D rows would never run it, and the generic catalog
would list it as a job capability that no route can start.

## What a side-by-side change needs

1. Replace the single id with a lookup of registry records whose `capability` is `mesh` and which pass the mesh profile.
   The route keeps no workflow name in code. A default is the first record in a fixed order (bundled first, then by id).
2. Let a request name the record. Options:
   - A. An optional `model` field on `POST /v1/3d/generations` and `/glb`, holding the record id. Fits the OpenAI-style
     routes and the existing model discovery. Omitted means the default.
   - B. A query parameter or header. Simpler to parse, but hidden from clients that read the model list.
   - C. An owner setting that picks the default record, with no per-request choice. Smallest change, still one engine at a time.
   Recommendation: A, with C as the default rule.
3. Make the "workflow available", catalog skip (`_gateway_catalog_finish`), facts (`_mesh_facts`) and pool probe
   (`mesh_api.pool_evidence`) work per record. Each record names its own service, so each can have its own pool.
   Two records on one GPU need one shared pool id, or the second request meets the first one's claim.
4. Tests: the existing second-workflow tests (`tests/mesh_workflows.py`) already build a data-only second document. Serve
   it beside the bundled one and prove each request reaches its own service.

## Snapshots that would move

Each changes only through its documented regeneration command, never by hand.

- OpenAPI and headless fixtures (`tests/fixtures/headless_openapi.json`, `tests/fixtures/c2d_parity/openapi.json`) with option A.
- Catalog and discovery fixtures (`tests/fixtures/c2d_parity/workflows_catalog.json`, `models_catalog.json`,
  `tests/fixtures/category_discovery_ungoverned.json`) once a second record can list.
- `tests/fixtures/route_graph_frozen.json` only if a route or its parameters change. Option A adds a parameter to existing
  routes. Do not regenerate it to make a test pass; the route-graph test names the review it needs.
- The test case ceiling in `tests/budgets.toml`, for the added cases.

Open questions for the owner: the choice between A, B and C; whether two mesh services may share one GPU pool; and how a
receipt records which record ran.

# Choosing between mesh workflows

## Built

The image-to-3D routes serve owner-configured registry records that have the mesh intent and pass the mesh profile.

- **Lookup.** `proxy._mesh_records()` takes the live registry's mesh records plus the bundled ones the live registry lacks,
  sorted by `(rank, id)`. `current_mesh_workflows()` and `current_mesh_workflow(id)` convert them once per record object.
- **Choice (option A).** A request names a record with the optional `model` field on `POST /v1/3d/generations` and
  `/glb`. The field holds a workflow id or its short form. A name that is not a mesh record is a 404.
- **First choice is data.** A request with no `model` runs the record with the lowest `selection_rank` in its policy facts
  (`registry/workflow_policy.yml`; `workflow_policy.rank_of`). `selection_rank` is an optional typed fact: a whole number from
  0 to 1,000,000 (a bool, a negative, a fraction or text is refused), one per record. A record with no rank sorts last; a tie
  goes to the lower id. The release file and the stored record validate it the same way (`facts_from_mapping`). An owner changes
  the first choice by writing `facts.selection_rank` on the record: `PUT /admin/api/workflows/{id}` (for a bundled id, the owner
  saves a copy under a new id, since a bundled id holds no stored contract), tested in `tests/test_workflow_owner_api.py`. The web owner view
  keeps the stored value and does not edit it yet. The shape asks `DEFAULT_WORKFLOWS` for it (`proxy._mesh_default_workflow`,
  the first ranked record the live registry holds), and `choose_workflow(prefer=...)` applies it only when more than one
  record fits the request. It never hides a missing-input refusal.
- **Availability, facts, probe.** Availability and discovery are evaluated per record. Workflows that share an exclusive
  pool run one at a time on that pool.
- **Receipt.** A receipt and a media job name the workflow that ran (`name_receipt(owner, entry.id, ...)`), so two records
  give two names.

Option B (query or header) and option C (an owner setting for the default only) were not built. C is now the `selection_rank` fact.

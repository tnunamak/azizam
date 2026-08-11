# Trellis mesh route design

Status: **Planned — not implemented.**  This document is the canonical authority for
v1.  It supersedes earlier Trellis designs and rejects the historical prototype as a
shippable implementation. This checkout is clean at `67a51bf`; it contains no
Trellis route, helper, module, or coupled tests. The rejected prototype is preserved
for fixture provenance only in local archive branch
`archive/trellis-prototype-0811` at commit
`c6afef0ac1749ddcf2a4d5d491f70395e569578d`. It is not product lineage and must never
be merged. The feature is GO only after this design is implemented and
the proof gates below pass; no route exists today.

## Decision and boundary

Implement exactly one source-defined `POST /v1/meshes/generate` bridge. It uses one
fixed workflow manifest with exactly one real service-ID binding, one closed
`http-multipart` POST, and returns raw GLB bytes. It is a stable protocol route, not a
dynamic workflow route, engine adapter, provider preset, plugin hook, route DSL,
transport registry, or second workflow catalog.

The route is executable only when the selected service declares a true
`exclusive-route-pool`. One `ExclusivePoolLeaseManager` instance per gateway process
serializes each pool from observation through downstream completion. Its guarantee is
limited to participating requests in that process. The supported deployment is one
gateway worker/process per physical exclusive pool; multi-process or multi-worker use
makes the declaration invalid. Shared-pool scheduling, workload buckets, learned
peaks, and interprocess coordination are outside v1.

The safe bootstrap seed has `capabilities: [mesh.generate]` and
`resource_capacity: {enabled: false}`. No machine-specific `max_claim_bytes` is
invented. An operator must explicitly establish that the pool is exclusive and set a
positive whole-envelope `max_claim_bytes` covering every request accepted by the
manifest before enabling capacity.

Engine knowledge remains data: workflow controls, mappings, service IDs, and declarative
transforms belong in validated records/manifests. Generic code must not contain engine
branches, engine parsers, engine remediation, or hard-coded workflow controls. A fixed
public mesh protocol and workflow reference are protocol/data facts, not engine code.

## Authoritative data

`registry/protocol_frontends.yml` alone owns method/path, protocol, intent, workflow
reference, required capability, exposure, trust/resource tiers, and public discovery.
The static handler reads the normalized record and fails closed before body parsing if it
is absent, malformed, or disagrees with its method/path; it has no defaults for these
facts. Authentication precedes route policy; selected-service policy precedes health,
observer, and downstream calls.

The bundled manifest owns logical inputs, defaults, ranges/enums, explicit `image` and
`mesh` modalities, `model/gltf-binary`, and mappings. It has exactly one binding keyed by
a real service ID. Its only transport descriptor is
`{kind: http-multipart, path: <canonical slash-prefixed relative path>}`. No method,
URL, headers, response map, selector, expression, callback, import, registry, or plugin
field is permitted. Required mappings are unique and flat. An optional input may be
unmapped; supplying it then fails as unsupported rather than being synthesized. The
stable public parts are `image` (required), `seed` (optional), and `extensions`
(optional JSON object); other fields are enumerated from the manifest.

The committed optional service remains in `proxy_config.yml`; no service-file move or
provider-preset catalog is added. Enabled capacity has a closed schema: mode exactly
`exclusive-route-pool`; non-empty `pool_id`; positive integer claim, reserve, retry,
and timeout values (booleans rejected); an existing observer service ID; canonical
relative observer path; and valid RFC-6901 free/total pointers. `enabled: false` is the
durable field tombstone; omitted provisioned capacity may be backfilled; deleting the
service record remains durable.

## Exact upgrade and rollback semantics

`registry.seed_upgrades` supports one operation only:
`exact_record_replace {upgrade_id, collection, record_id, expected_hash,
replacement_record, replacement_hash}`. `collection` is a closed enum of the existing
normalized registry collections needed here. IDs must match, and the complete
replacement must pass the normal validator. There is no path, append, merge, delete,
callback, expression, template, or arbitrary operation name: this is exact whole-record
seed CAS, not a patch DSL.

The OpenAI frontend correction and clean old Trellis service correction use this same
primitive. Known preconditions are frontend hash
`e933a0546958ed5554b76cde94a8ba1f0b88ad33ec97b121b4a8e83d5da79119` and dirty seed
provenance hash `b191ec76060d957cf477ab877a7f057fbab476a0a08bcc3479ede257e43b2af7`.
Missing/deleted or non-matching records produce `skipped_drift` and are never recreated
or partially changed. A replacement hash produces `already_applied`; otherwise an exact
match produces `applied`.

Replacement, terminal upgrade result, registry generation, and audit event commit
atomically under the existing CAS transaction. Results are keyed by `upgrade_id`; later
boots do not rerun or re-audit terminal upgrades. Audit stores only upgrade ID,
collection/record IDs, status, and before/after hashes—never record bodies, headers, or
secrets. Rollback is registry-generation restore only; there is no reverse-patch
interpreter.

## Runtime safety and lifecycle

Resolution order is: valid static route and manifest; exactly one binding; service
exists/enabled; capability; operator lifecycle; full backend policy; generic health;
capacity for execution only. Discovery shares this resolver but does not acquire
capacity. Health receives the selected live service configuration explicitly.

The lease acquires its per-pool `asyncio.Lock` before observation and holds it through
downstream completion and cleanup. Different pools proceed independently; waiting is
cancellable and there is no queue, retry loop, scheduler, or try-lock. Observer values
are integers (not booleans), finite, `total > 0`, `0 <= free <= total`. Use
`reserve_mb = ceil(reserve_bytes/MiB)`, `claim_mb = ceil(max_claim_bytes/MiB)`,
`total_vram_mb = floor(total/MiB)`, and
`used_vram_mb = ceil((total-free)/MiB) + reserve_mb` when constructing the existing
`CapacitySnapshot` and `ResourceClaim`. Record the accepted request/claim before yield;
remove that same claim exactly once in `finally`.

`ReceiptLifecycleOwner` starts after auth and route policy but before validation or I/O
and is the sole terminal receipt writer. Form parsing exclusively owns ASGI `receive`.
Only after the complete multipart body is consumed may the single persistent disconnect
watcher start; it must not poll `is_disconnected` or race another receiver. On
disconnect it cancels and awaits the one HTTP task, closes response/client resources,
and re-raises `CancelledError`; the lease exits before receipt cancellation. Form,
upload, response, client, task, and file cleanup is required on every path.

`gateway/http_multipart.py` accepts only validated typed assets and flat scalar parts,
serializes string/bool/integer/finite-float values canonically, performs one non-retried
POST, bounds discarded upstream bodies, and returns raw success bytes or typed
`connect|timeout|upstream|malformed` failure. It has no GLB or engine-error parser.

## Route override and discovery

The sole admin resource is `GET` and `PUT /admin/api/route-overrides/mesh-generate`,
under existing admin auth, ETag/CAS, audit, and redaction rules. GET returns generation/
ETag, bundled manifest/hash, stored override, effective manifest/binding, activation
mode `restart_required`, and a typed stale/invalid reason; it returns no secret headers.
PUT accepts only expected generation/ETag, an existing service ID, canonical transport
path, and mappings enumerated from the bundled manifest. It accepts no route, method,
workflow ID, policy, arbitrary manifest, second candidate, code symbol, or plugin.
It clones the bundle, replaces its sole binding atomically, stamps exact route-override
provenance, and stores the current base hash. Only enabled `source: ui`, exact route/
workflow scope, matching base hash may shadow the bundle. Imported, discovered,
provisioned, disabled, malformed, wrong-scope, or stale records cannot shadow. A stale
override fails the route closed and makes discovery unavailable until re-saved; it does
not fall back silently.

Discovery adds optional `protocol` and `output_media_types` to `Capability`, preserving
them through normalization. The mesh row reports `openai.mesh`, image input, mesh
output, `model/gltf-binary`, workflow ID, and audience-appropriate service ID. Awaitable
providers are awaited at both capability endpoint call sites; only this mesh join adds
the live probe. Eligible plus successful 2xx means `lifecycle=warm, health=healthy`.
Disabled, maintenance, draining, missing, incapable, timeout, missing probe facts,
non-2xx, exception, or unknown is unavailable with a bounded reason and no raw response.
Tiers come only from the route record.

## Public outcomes and proof contract

403 means route/service policy denial; 400 means invalid multipart, seed, extension, or
manifest input. Missing, disabled, incapable, ineligible, unhealthy, absent/disabled
exclusivity, invalid/stale override, or disabled capacity is `503 service_unavailable`
without `Retry-After`. Malformed/unavailable observer or insufficient claim-plus-reserve
is `503 resource_unavailable` with configured `Retry-After`. Downstream connect/non-2xx
is 502, timeout is 504, and cancellation is re-raised. Logs, receipts, errors, and
audits contain only bounded identifiers and outcome classes, never bytes, raw bodies,
resolved headers, credentials, or secrets.

The following 50 acceptance items are binding:

1. One source-defined handler; no data/package/plugin route registration or shadowing.
2. Protocol frontend is sole owner of route policy/discovery facts.
3. Handler reads normalized facts and fails closed without defaults.
4. Auth, route policy, then candidate policy precede body/I/O respectively.
5. Upgrades use only exact whole-record replace.
6. Collection is closed; IDs and replacement validation are exact.
7. No patch-DSL operation or field mutation exists.
8. Drift skips, deletion persists, replacement is idempotent.
9. Record/result/generation/audit are atomic and redacted.
10. Rollback restores an audited registry generation only.
11. Optional service remains in `proxy_config.yml`.
12. Safe bootstrap is capabilities plus capacity disabled.
13. Enabled capacity uses the complete closed exclusive schema.
14. Omission may backfill; false tombstones and deletion persist.
15. UI labels the whole-envelope operator safety assertion.
16. Manifest owns one real service-ID binding and all logical data.
17. Transport is only closed relative `http-multipart`.
18. Required targets are unique; unmapped optional supplied fields reject.
19. Production generic code/UI has no engine/control literals.
20. One per-process manager keyed only by opaque pool ID.
21. Lock precedes observation and spans downstream cleanup.
22. Guarantee is process-local; multi-worker sharing is invalid.
23. Observer validation, rounding, reserve equation, and release are exact.
24. False/absent capacity never dispatches; no cache, bucket, sampler, or learning.
25. Receipt owner starts before validation/I/O and owns terminality.
26. Form exclusively owns receive; watcher starts after full body consumption.
27. Disconnect cancels/awaits HTTP and lease cleanup in order.
28. All cleanup paths pass warnings-as-errors tests.
29. Multipart executor is typed, bounded, canonical, and non-retrying.
30. Override resource has exact GET and PUT paths and existing auth/CAS/audit.
31. GET returns the complete specified read model and no secrets.
32. PUT accepts only scoped service/path/mappings and expected CAS.
33. PUT clones bundle, replaces one binding, and stores provenance/base hash.
34. Only enabled exact scoped fresh UI records shadow.
35. Stale scope fails closed; no silent fallback.
36. Generic registry validation stays route-literal-free.
37. Route/discovery share ordered resolution; discovery skips capacity.
38. Health uses selected live config and shared semantics.
39. Capability preserves optional protocol/media fields.
40. Awaitable capability providers are awaited; existing probes stay unchanged.
41. Discovery state mapping is exactly the mapping above.
42. Discovery owns tiers only through route metadata.
43. Public 403/400/503 service-unavailable mapping is exact.
44. Resource/downstream/cancellation outcomes and retry behavior are exact.
45. Observability is bounded and secret-free.
46. Before deleting the historical prototype handler, preserve deterministic fixtures
    for public form fields, namespace validation, filename/content type, omitted
    defaults, scalar mapping, success status/content type/GLB bytes, and ordinary
    400/502/504 classifications. Record intentional changes: negative/boolean seed
    rejection, fail-closed observer behavior, the exclusive-capacity requirement,
    removal of learning/cache behavior, and bounded generic upstream errors. The
    fixture source is the archived prototype commit named above; no prototype files
    exist in this clean checkout today.
47. Delete historical-prototype Trellis constants/functions, direct observer/cache
    code, `gateway/mesh_extensions.py`, and the two implementation-coupled tests
    (`tests/test_mesh_generate_route.py` and `tests/test_trellis_vram_gate.py`) after
    their valid fixtures move. Do not preserve their fail-open or false-peak
    assertions. These named files are absent from the current HEAD; this is a future
    implementation disposition, not a request to recreate or delete them now.
48. Focused deterministic tests cover exact upgrade outcomes, disabled bootstrap,
    deletion/tombstone behavior, stale override, an arbitrary compatible service ID,
    async discovery, same-pool serialization, different-pool progress, numeric
    boundaries, every cancellation await point, receipt terminality, canonical
    path/scalar rules, and the full ASGI fake journey. Cancellation coverage includes
    lock wait, observer, downstream connect/write/read, disconnect watcher, HTTP-task
    cancellation/await, response/client close, upload/form cleanup, and lease release
    boundaries. No test contacts ports 5000/15000 or a live backend.
49. Run warning-as-error focused tests, full `./venv/bin/python -m pytest -q`,
    `git diff --check`, engine-boundary/literal sweeps, and reread all touched files;
    independent review is required before implementation is accepted.
50. In any future implementation worktree, preserve every unrelated dirty file and
    hunk; never reset, clean, stash, overwrite, or fold unrelated work into the
    Trellis change. This is a preservation rule for that future worktree, not a claim
    that the current HEAD is dirty.

## Implementation order, rollback, and stop condition

1. Freeze historical-prototype request/output fixtures and classify intentional behavior changes.
2. Add data, manifest validation, exact seed CAS, deletion/tombstone tests.
3. Build/test the closed multipart executor byte-for-byte.
4. Build/test the exclusive lease with same/different-pool and cancellation barriers.
5. Replace the route with receipt, post-body watcher, resolver, lease, and executor.
6. Add scoped GET/PUT override and asynchronous discovery.
7. Delete historical-prototype helpers/tests, run boundary/literal sweeps, full tests, reread, and
   obtain independent final diff review.

Rollback restores the audited registry generation and reverts the implementation commit
as a normal reviewed change; never interpret reverse patches or restore deleted dirty
code. Stop binary: if any acceptance item 1–50 is false, if the exclusive pool cannot
be truthfully declared, if any required deterministic test is absent/fails, or if an
engine-specific code branch/control literal appears, v1 is unavailable and no route is
claimed. Do not ship a partial safety claim.

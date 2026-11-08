"""What the 3D routes' workflow looks like to discovery and to the availability probe.

The routes themselves are frontend rows (``registry/protocol_frontends.yml``: ``mesh-generations`` and ``mesh-glb``)
served over the run core, and the engine call, the pool lease and the observer are the multipart adapter
(``gateway/multipart_adapter.py``). What stays here is read-only: the workflow record as the 3D wire sees it
(``MeshWorkflow``), the facts the capability catalog lists it with (``discovery_facts``) and the pool's state as
availability evidence (``pool_evidence``). Nothing here sends a request to a caller's engine or holds a lease.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

import httpx

from gateway.exclusive_pool import MIB, ExclusivePoolSpec
from gateway.frontend_shapes.mesh import mesh_profile
from gateway.mesh_manifest import MESH_INTENT, MESH_MEDIA_TYPE, MeshManifestError
from gateway.multipart_adapter import (
    SERVICE_ABSENT,
    SERVICE_DISABLED,
    config_of,
    exclusive_pool_problem,
    observer_problem,
    probe_pool,
)
from gateway.capabilities import EXPOSURE_CLASSES, RESOURCE_TIERS, TRUST_TIERS, Capability, ResourceEstimate
from gateway.capability_ids import CAPABILITY_LABELS
from gateway.service_registry import service_base_url

FRONTEND_ID = "mesh"
CAPABILITY = "mesh"
MESH_PROTOCOL = "openai.mesh"
# The route the record's tiers are read from (the catalog row lists the 3D routes by these tiers).
LISTING_PATH = "/v1/3d/generations"
MAX_IMAGE_BYTES = 25 * MIB
# An answer of the availability probe younger than this is served as it is. An older one is still served, while one refresh
# runs behind it, so a slow or hung host delays only the first listing.
POOL_PROBE_FRESH_SECONDS = 10.0
FRONTEND_ABSENT = "frontend record is absent"
FRONTEND_DISABLED = "frontend record is disabled"
WORKFLOW_ABSENT = "workflow record is absent or disabled"
# A route that is not set up, or that the owner turned off, is Off on every surface, not a fault: its evidence says so
# in the availability vocabulary (availability.project_service).
_SETUP_STATES = {FRONTEND_ABSENT: {"configured": False}, SERVICE_ABSENT: {"configured": False},
                 FRONTEND_DISABLED: {"enabled": False}, SERVICE_DISABLED: {"enabled": False},
                 WORKFLOW_ABSENT: {"enabled": False}}


@dataclass
class PoolProbeCache:
    """The availability probe's answers, one per probed pair of URLs (health, observer), and at most one probe in flight
    per pair. Each MeshApiDeps owns one; the gateway's deps are built once, so its cache is process-wide."""

    # (health URL, observer URL) -> (time.monotonic() when the probe finished, epoch second it finished,
    # the reason or None when the pool is ready)
    answers: dict[tuple[str, str], tuple[float, int, str | None]] = field(default_factory=dict)
    inflight: dict[tuple[str, str], asyncio.Task] = field(default_factory=dict)



@dataclass(frozen=True)
class MeshWorkflow:
    """The mesh route's view of its workflow record: read from the schema 2 contract (CODE_TO_DATA_S3, S3.4, D9) after the
    mesh frontend profile accepted it."""

    id: str
    intent: str
    version: str
    manifest_hash: str
    inputs: Mapping[str, Mapping[str, Any]]
    service_id: str
    transport_path: str
    mapping: Mapping[str, str]
    output_media_type: str
    data: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def image_input(self) -> str:
        return next(name for name, spec in self.inputs.items() if spec.get("type") == "asset")

    @property
    def takes_text(self) -> bool:
        return False  # the route takes one picture and scalar options; a text prompt is not one of them

    def mapped_inputs(self) -> tuple[str, ...]:
        return tuple(self.mapping)

    @property
    def max_image_bytes(self) -> int:
        """What the route accepts of the picture: its own bound, or the picture input's ``max_bytes`` when that is lower."""
        stated = self.inputs[self.image_input].get("max_bytes")
        return min(MAX_IMAGE_BYTES, stated) if isinstance(stated, int) and not isinstance(stated, bool) else MAX_IMAGE_BYTES


def mesh_profile_errors(contract: Mapping[str, Any]) -> list[str]:
    """Why this route cannot serve the workflow: the mesh wire's profile (``frontend_shapes.mesh``) and the intent the
    route's own workflow loader requires. Empty when it can."""
    problems = mesh_profile(contract)
    if isinstance(contract, Mapping) and contract.get("intent") != MESH_INTENT:
        problems.insert(0, f"intent must be {MESH_INTENT!r}")
    return problems




def mesh_workflow(contract: Mapping[str, Any], manifest_hash: str) -> MeshWorkflow:
    """The route's workflow from a registry record's contract. Raises ``MeshManifestError`` when the profile refuses it."""
    problems = mesh_profile_errors(contract)
    if problems:
        raise MeshManifestError("mesh workflow does not fit the mesh frontend: " + "; ".join(problems))
    binding = next(iter(contract["bindings"].values()))
    (output,) = contract["outputs"].values()
    return MeshWorkflow(
        id=str(contract["id"]), intent=MESH_INTENT, version=str(contract["version"]), manifest_hash=manifest_hash,
        inputs=contract["inputs"], service_id=binding["service"], transport_path=binding["path"],
        mapping=dict(binding["map"]), output_media_type=output["media_type"], data=contract)


def mesh_workflow_from_record(record: Any) -> MeshWorkflow:
    """The mesh route's workflow from its registry record, after the mesh frontend profile accepted the record's contract."""
    return mesh_workflow(record.contract, record.identity.manifest_hash)




@dataclass
class MeshApiDeps:
    """What the availability probe reads from the gateway. Tests inject fakes for each of them."""

    # Zero-argument: the workflow record as it is now. Each listing calls it once and uses that one value throughout, so a
    # refresh in the middle cannot mix two records.
    current_manifest: Callable[[], MeshWorkflow]
    # The normalized frontend records (live registry, else seed).
    frontend_records: Callable[[], list[Mapping[str, Any]]]
    # Service ID -> normalized service record (live registry).
    services: Callable[[], Mapping[str, Mapping[str, Any]]]
    # A fresh client per probe, closed when the probe ends.
    client_factory: Callable[[], httpx.AsyncClient]
    # Answers of the availability probe only (pool_evidence).
    probe_cache: PoolProbeCache = field(default_factory=PoolProbeCache)
    # workflow_available() -> whether the live registry holds the workflow record now. A record that is absent or
    # disabled is a state of the route, never a failure to start. None means always.
    workflow_available: Callable[[], bool] | None = None


def route_row(records: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any] | None, str | None]:
    """The frontend record's listing route and why it is not usable, or (None, reason). Reads no default."""
    record = next((item for item in records if item.get("id") == FRONTEND_ID), None)
    if record is None:
        return None, FRONTEND_ABSENT
    if record.get("enabled") is not True:
        return None, FRONTEND_DISABLED
    rows = [row for row in record.get("registered_routes") or ()
            if isinstance(row, Mapping) and str(row.get("canonical") or "").strip() == LISTING_PATH]
    if len(rows) != 1:
        return None, "frontend record must hold exactly one row for the route"
    row = rows[0]
    methods = row.get("methods")
    if not isinstance(methods, list) or [str(m).upper() for m in methods] != ["POST"]:
        return None, "frontend row must name only POST"
    if row.get("intent") != MESH_INTENT or row.get("capability") != CAPABILITY:
        return None, "frontend row names another intent or capability"
    tiers = row.get("runtime_policy")
    if not isinstance(tiers, Mapping) or tiers.get("protocol") != MESH_PROTOCOL:
        return None, "frontend row names another protocol"
    if (tiers.get("capability_exposure") not in EXPOSURE_CLASSES
            or tiers.get("capability_trust_tier") not in TRUST_TIERS
            or tiers.get("capability_resource_tier") not in RESOURCE_TIERS):
        return None, "frontend row has no valid route tiers"
    return row, None


async def registry_snapshot(deps: MeshApiDeps) -> MeshApiDeps:
    """deps whose registry readers return one snapshot, read off the event loop. The readers query the admin store, so
    a read on the loop stalls every request; one read also keeps the route row, the service and the observer from
    different registry versions. The probe cache is shared, not copied."""
    frontends, services = await asyncio.to_thread(lambda: (deps.frontend_records(), deps.services()))
    return replace(deps, frontend_records=lambda: frontends, services=lambda: services)



def discovery_facts(manifest: MeshWorkflow, frontend_records: Sequence[Mapping[str, Any]]) -> Capability:
    """The mesh row's facts for the shared capability catalog: what the route is, and the tiers its frontend row sets
    (or the defaults, when that row is not usable). It reads nothing and holds no state. The state is the pool's probe,
    which joins as evidence (pool_evidence), so a route whose frontend or service is missing still lists, as Off."""
    row, _ = route_row(frontend_records)
    tiers = (row or {}).get("runtime_policy") or {}
    return Capability(
        intent=MESH_INTENT,
        backend=manifest.service_id,
        workflow=manifest.id,
        capability=CAPABILITY,
        title=CAPABILITY_LABELS[CAPABILITY],
        input_modalities=("image",),
        output_modalities=("mesh",),
        execution="sync",
        protocol=MESH_PROTOCOL,
        output_media_types=(MESH_MEDIA_TYPE,),
        exposure=tiers.get("capability_exposure", "disabled"),
        trust_tier=tiers.get("capability_trust_tier", "experimental"),
        resource_tier=tiers.get("capability_resource_tier", "batch"),
        resources=ResourceEstimate(gpu_required=True),
    )


async def pool_evidence(deps: MeshApiDeps) -> dict[str, dict[str, Any]]:
    """The pool's state as availability evidence, keyed by its service: the bounded reason the route cannot be used,
    or the answer of one live probe of the service health and the observer's facts. A route that is not set up or is
    turned off says so (``configured`` or ``enabled`` false). It acquires no capacity, dispatches nothing, and reports
    a reason rather than raising."""
    manifest = deps.current_manifest()
    workflow_present = deps.workflow_available is None or deps.workflow_available()
    deps = await registry_snapshot(deps)
    _, problem = route_row(deps.frontend_records())
    if not workflow_present:
        problem = WORKFLOW_ABSENT
    observed_s = int(time.time())
    if problem is None:
        problem, spec, service = exclusive_pool_problem(deps.services(), manifest.service_id, CAPABILITY)
        if problem is None and spec is not None and service is not None:
            problem, observed_s = await _pool_answer(deps, spec, service)
    available = problem is None
    return {manifest.service_id: {
        "available": available,
        "status": "ready" if available else "unavailable",
        "reason": "Service health and observer facts answered." if available else problem,
        "observed_at_s": observed_s,
        "source": "live_probe",
        **_SETUP_STATES.get(problem or "", {}),
    }}


async def _pool_answer(deps: MeshApiDeps, spec: ExclusivePoolSpec, service: Mapping[str, Any]) -> tuple[str | None, int]:
    """The reason the pool cannot take work now (None when it can), and the epoch second it was observed, from the probe
    cache. A pair never probed waits for its first probe, which every listing that arrives meanwhile shares. A cached
    answer returns at once; once it is older than POOL_PROBE_FRESH_SECONDS, one refresh starts behind it. A refresh that
    is already in flight is not doubled."""
    config = config_of(service)
    base = service_base_url(config)
    if not base:
        return "service has no base URL", int(time.time())
    observer_issue, observer_url = observer_problem(deps.services(), spec)
    if observer_issue is not None:
        return observer_issue, int(time.time())
    key = (base + str(config.get("health_endpoint") or "/health"), observer_url)
    answer = deps.probe_cache.answers.get(key)
    if answer is None:
        return await asyncio.shield(_pool_probe_task(deps, spec, service, key))
    finished, observed, reason = answer
    if time.monotonic() - finished >= POOL_PROBE_FRESH_SECONDS:
        _pool_probe_task(deps, spec, service, key)
    return reason, observed


def _pool_probe_task(deps: MeshApiDeps, spec: ExclusivePoolSpec, service: Mapping[str, Any],
                     key: tuple[str, str]) -> asyncio.Task:
    """The probe in flight for this pair, or a new one. Shielded by its callers, so a cancelled listing never cancels it."""
    task = deps.probe_cache.inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(_run_pool_probe(deps, spec, service, key))
        deps.probe_cache.inflight[key] = task
    return task


async def _run_pool_probe(deps: MeshApiDeps, spec: ExclusivePoolSpec, service: Mapping[str, Any],
                          key: tuple[str, str]) -> tuple[str | None, int]:
    """Probe the pool once and record the answer. Never raises: a probe that fails is a reason on the row."""
    try:
        try:
            reason = await probe_pool(deps, spec, service)
        except Exception as error:  # noqa: BLE001 - the row carries one bounded reason, never the raw error
            reason = f"probe failed: {type(error).__name__}"
        observed = int(time.time())
        deps.probe_cache.answers[key] = (time.monotonic(), observed, reason)
        return reason, observed
    finally:
        deps.probe_cache.inflight.pop(key, None)

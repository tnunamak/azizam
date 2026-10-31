"""POST /v1/meshes/generate: one image in, one GLB out, through the bundled mesh manifest.

The handler is source-defined and owned by the ``mesh`` protocol frontend. Order of checks:

1. authentication (401/403 come from the caller's own policy, unchanged);
2. the frontend record for this route: it must exist, be enabled, and say exactly this method, path,
   intent, workflow and capability, or the route fails closed (503) before the body is read;
3. the selected service: exists, enabled, declares the capability, and declares an enabled exclusive
   pool, or 503 before the body is read;
4. the receipt opens, then the multipart body is read and bound to the manifest (400 on any problem);
5. the service's health (503), then the exclusive pool lease: its observer, the VRAM claim, and the
   downstream POST, all inside one lease held from observation until cleanup;
6. the raw GLB bytes, or a typed 502/504 for downstream failure.

Form parsing owns the ASGI receive channel. A single disconnect watcher starts only after the body is
fully read; on disconnect it cancels and awaits the one HTTP task, which releases the lease first, and
the receipt owner then records the cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from gateway.exclusive_pool import (
    MIB,
    ExclusivePoolLeaseManager,
    ExclusivePoolSpec,
    PoolCapacityError,
    PoolObserverError,
    PoolObservation,
    observation_from_document,
    parse_resource_capacity,
)
from gateway.http_multipart import (
    KIND_CONNECT,
    KIND_MALFORMED,
    KIND_TIMEOUT,
    KIND_UPSTREAM,
    MultipartFailure,
    post_multipart,
)
from gateway.mesh_manifest import (
    MESH_INTENT,
    MESH_MEDIA_TYPE,
    MeshInputError,
    MeshManifest,
    bind_request,
    downstream_form,
    sniff_image,
)
from gateway import client_errors, operation_security
from gateway.availability import AvailabilityProjection
from gateway.capabilities import EXPOSURE_CLASSES, RESOURCE_TIERS, TRUST_TIERS, Capability, ResourceEstimate
from gateway.capability_ids import CAPABILITY_LABELS
from gateway.request_forms import read_form
from gateway.service_registry import service_base_url

LOG = logging.getLogger("azizam.mesh")

MESH_PATH = "/v1/meshes/generate"
FRONTEND_ID = "mesh"
CAPABILITY = "mesh"
MESH_PROTOCOL = "openai.mesh"
MAX_IMAGE_BYTES = 25 * MIB
MAX_GLB_BYTES = 512 * MIB
_HEALTH_TIMEOUT_SECONDS = 5.0
# An answer of the discovery probe younger than this is served as it is. An older one is still served, while one refresh
# runs behind it, so a slow or hung trellis host delays only the first listing.
POOL_PROBE_FRESH_SECONDS = 10.0


@dataclass
class PoolProbeCache:
    """The discovery probe's answers, one per probed pair of URLs (health, observer), and at most one probe in flight
    per pair. Each MeshApiDeps owns one; the gateway's deps are built once, so its cache is process-wide."""

    # (health URL, observer URL) -> (time.monotonic() when the probe finished, the reason or None when the pool is ready)
    answers: dict[tuple[str, str], tuple[float, str | None]] = field(default_factory=dict)
    inflight: dict[tuple[str, str], asyncio.Task] = field(default_factory=dict)


@dataclass(frozen=True)
class MeshApiDeps:
    """Everything the handler reads from the gateway. Tests inject fakes for each of them."""

    manifest: MeshManifest
    # Synchronous: the gateway's authenticator. Returns the caller's context, or the refusal to send.
    authenticate: Callable[[Request, str], Any]
    # The normalized frontend records (live registry, else seed).
    frontend_records: Callable[[], list[Mapping[str, Any]]]
    # Service ID -> normalized service record (live registry).
    services: Callable[[], Mapping[str, Mapping[str, Any]]]
    # open_receipt(auth, request_id, path) -> an async context manager with report(status=, reason=).
    open_receipt: Callable[[Any, str, str], Any]
    leases: ExclusivePoolLeaseManager
    # A fresh client per request, closed when the request's work ends or is cancelled.
    client_factory: Callable[[], httpx.AsyncClient]
    new_request_id: Callable[[], str]
    # route_policy(auth, frontend_row) -> a policy decision for this caller on this route (the same evaluator the
    # other Try workflows use). A decision with ``denied`` true refuses the request with 403 before the body is read.
    route_policy: Callable[[Any, Mapping[str, Any]], Any]
    # Answers of the capability listing's probe only; the request path always probes live.
    probe_cache: PoolProbeCache = field(default_factory=PoolProbeCache)


def _retry_after_headers(spec: ExclusivePoolSpec) -> dict[str, str]:
    """``Retry-After`` for a busy pool, written as the number the gateway chose (client_errors.retry_after)."""
    seconds = client_errors.retry_after(str(spec.retry_after_seconds))
    return {"Retry-After": seconds} if seconds is not None else {}


def _error(status: int, code: str, message: str, *, param: str | None = None,
           headers: Mapping[str, str] | None = None, request_id: str | None = None) -> JSONResponse:
    error: dict[str, Any] = {"message": message, "type": "invalid_request_error", "code": code}
    if param is not None:
        error["param"] = param
    merged = dict(headers or {})
    if request_id:
        merged["x-request-id"] = request_id
    return JSONResponse({"error": error}, status_code=status, headers=merged)


def _config_of(service: Mapping[str, Any] | None) -> Mapping[str, Any]:
    value = (service or {}).get("config")
    return value if isinstance(value, Mapping) else {}


def _unavailable(request_id: str) -> JSONResponse:
    return _error(503, "service_unavailable", "This route is not available.", request_id=request_id)


def route_row(records: list[Mapping[str, Any]], manifest: MeshManifest) -> tuple[Mapping[str, Any] | None, str | None]:
    """The frontend row for this route and why it is not usable, or (None, reason). Reads no default."""
    record = next((item for item in records if item.get("id") == FRONTEND_ID), None)
    if record is None:
        return None, "frontend record is absent"
    if record.get("enabled") is not True:
        return None, "frontend record is disabled"
    rows = [row for row in record.get("registered_routes") or ()
            if isinstance(row, Mapping) and str(row.get("canonical") or "").strip() == MESH_PATH]
    if len(rows) != 1:
        return None, "frontend record must hold exactly one row for the route"
    row = rows[0]
    methods = row.get("methods")
    if not isinstance(methods, list) or [str(m).upper() for m in methods] != ["POST"]:
        return None, "frontend row must name only POST"
    if row.get("intent") != MESH_INTENT or row.get("workflow") != manifest.id or row.get("capability") != CAPABILITY:
        return None, "frontend row disagrees with the bundled manifest"
    tiers = row.get("runtime_policy")
    if not isinstance(tiers, Mapping) or tiers.get("protocol") != MESH_PROTOCOL:
        return None, "frontend row names another protocol"
    if (tiers.get("capability_exposure") not in EXPOSURE_CLASSES
            or tiers.get("capability_trust_tier") not in TRUST_TIERS
            or tiers.get("capability_resource_tier") not in RESOURCE_TIERS):
        return None, "frontend row has no valid route tiers"
    return row, None


def service_problem(services: Mapping[str, Mapping[str, Any]], manifest: MeshManifest) -> tuple[str | None, ExclusivePoolSpec | None, Mapping[str, Any] | None]:
    """The selected service's policy, before the body is read: (problem, exclusive pool spec, service)."""
    service = services.get(manifest.service_id)
    if not isinstance(service, Mapping):
        return "service is absent", None, None
    if service.get("enabled") is False:
        return "service is disabled", None, service
    capabilities = service.get("capabilities") or ()
    if CAPABILITY not in capabilities:
        return "service lacks the capability", None, service
    config = _config_of(service)
    try:
        spec = parse_resource_capacity(manifest.service_id, config.get("resource_capacity"))
    except ValueError as error:
        return str(error), None, service
    if spec is None:
        return "exclusive capacity is not enabled", None, service
    return None, spec, service


def register_mesh_routes(app: FastAPI, deps: MeshApiDeps) -> None:
    manifest = deps.manifest

    @app.post(MESH_PATH, openapi_extra=operation_security.openapi_extra(operation_security.APP_KEY))
    async def generate_mesh(request: Request) -> Response:
        request_id = deps.new_request_id()
        gate = await asyncio.to_thread(deps.authenticate, request, MESH_PATH)
        if isinstance(gate, Response):
            return gate
        facts = await registry_snapshot(deps)
        row, record_problem = route_row(facts.frontend_records(), manifest)
        if row is None:
            LOG.warning("mesh route refused: %s", record_problem)
            return _unavailable(request_id)
        problem, spec, service = service_problem(facts.services(), manifest)
        if problem is not None or spec is None or service is None:
            LOG.warning("mesh route refused: %s", problem)
            return _unavailable(request_id)
        decision = deps.route_policy(gate, row)
        if decision is not None and decision.denied:
            return _policy_refusal(decision, request_id)
        observer = _observer_problem(facts, spec)
        async with deps.open_receipt(gate, request_id, MESH_PATH) as owner:
            return await _serve(request, request_id, owner, service, spec, observer)

    async def _serve(request: Request, request_id: str, owner: Any, service: Mapping[str, Any],
                     spec: ExclusivePoolSpec, observer: tuple[str | None, str]) -> Response:
        try:
            form = await read_form(request, max_files=2, max_fields=len(manifest.inputs) + 2,
                                   max_part_size=MAX_IMAGE_BYTES + 1)
        except Exception:  # noqa: BLE001 - any malformed or oversized multipart is one 400
            owner.report(status="failed", reason="invalid_multipart")
            return _error(400, "invalid_multipart", "The request body is not valid multipart form data.",
                          request_id=request_id)
        try:
            supplied, image_part = _split_form(form, manifest)
            image = sniff_image(await image_part.read(MAX_IMAGE_BYTES + 1), max_bytes=MAX_IMAGE_BYTES) \
                if image_part is not None else None
            mesh_request = bind_request(manifest, supplied, image)
        except MeshInputError as error:
            owner.report(status="failed", reason=error.code)
            return _error(400, error.code, str(error), param=error.param, request_id=request_id)
        finally:
            await form.close()
        fields, image_asset = downstream_form(manifest, mesh_request)

        return await _watch_and_run(request, request_id, owner, service, spec, observer, fields, image_asset)

    async def _watch_and_run(request: Request, request_id: str, owner: Any, service: Mapping[str, Any],
                             spec: ExclusivePoolSpec, observer: tuple[str | None, str], fields: Mapping[str, str],
                             image: Any) -> Response:
        config = _config_of(service)
        base = service_base_url(config)
        if not base:
            owner.report(status="failed", reason="service_unavailable")
            return _unavailable(request_id)
        async with deps.client_factory() as client:
            health_url = base + str(config.get("health_endpoint") or "/health")
            try:
                healthy = await client.get(health_url, timeout=_HEALTH_TIMEOUT_SECONDS)
            except httpx.HTTPError:
                healthy = None
            if healthy is None or not 200 <= healthy.status_code < 300:
                owner.report(status="failed", reason="unhealthy")
                return _unavailable(request_id)

            work = asyncio.ensure_future(_lease_and_post(client, request_id, base, config, spec, observer, fields,
                                                         image))
            watcher = asyncio.ensure_future(_watch_disconnect(request))
            try:
                done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                work.cancel()
                watcher.cancel()
                await asyncio.gather(work, watcher, return_exceptions=True)
                raise
            if work in done:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                return await _finish(work, owner, request_id, spec)
            # The client went away first: cancel the HTTP task and await it. Its lease exits inside that task,
            # before the receipt owner records the cancellation.
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise asyncio.CancelledError()

    async def _lease_and_post(client: httpx.AsyncClient, request_id: str, base: str, config: Mapping[str, Any],
                              spec: ExclusivePoolSpec, observer: tuple[str | None, str], fields: Mapping[str, str],
                              image: Any) -> bytes:
        observer_problem, observer_url = observer

        async def observe() -> Any:
            if observer_problem is not None:
                raise PoolObserverError(observer_problem)
            try:
                response = await client.get(observer_url, timeout=spec.timeout_seconds)
            except httpx.HTTPError as error:
                raise PoolObserverError("observer did not answer") from error
            if response.status_code != 200:
                raise PoolObserverError("observer did not answer")
            try:
                return response.json()
            except ValueError as error:
                raise PoolObserverError("observer answer is not JSON") from error

        async with deps.leases.lease(spec, request_id=request_id, observe=observe):
            return await post_multipart(
                client,
                base + manifest.transport_path,
                image_field=manifest.mapping[manifest.image_input],
                image=image,
                fields=fields,
                timeout_seconds=float(config.get("timeout") or 900),
                max_response_bytes=MAX_GLB_BYTES,
            )

    async def _finish(work: asyncio.Future, owner: Any, request_id: str, spec: ExclusivePoolSpec) -> Response:
        try:
            glb = work.result()
        except PoolCapacityError as error:
            owner.report(status="failed", reason="capacity")
            LOG.info("mesh capacity refused: %s", error.status.value)
            return _error(503, "resource_unavailable", "The GPU is busy. Try again shortly.",
                          headers=_retry_after_headers(spec), request_id=request_id)
        except PoolObserverError:
            owner.report(status="failed", reason="observer")
            return _error(503, "resource_unavailable", "The GPU is busy. Try again shortly.",
                          headers=_retry_after_headers(spec), request_id=request_id)
        except MultipartFailure as failure:
            return _downstream_failure(owner, request_id, failure)
        except Exception:  # noqa: BLE001 - an unexpected fault is one bounded 500; detail stays in the log
            LOG.exception("mesh route fault")
            owner.report(status="failed", reason="internal")
            return _error(500, "internal_error", "The mesh route failed.", request_id=request_id)
        owner.report(status="succeeded", reason="succeeded")
        return Response(content=glb, media_type=MESH_MEDIA_TYPE, headers={"x-request-id": request_id})

    def _downstream_failure(owner: Any, request_id: str, failure: MultipartFailure) -> Response:
        owner.report(status="failed", reason=failure.kind)
        LOG.warning("mesh downstream failed: %s", failure)
        if failure.kind == KIND_TIMEOUT:
            return _error(504, "upstream_timeout", "The mesh service did not answer in time.", request_id=request_id)
        if failure.kind == KIND_CONNECT:
            return _error(502, "upstream_unavailable", "The mesh service could not be reached.", request_id=request_id)
        if failure.kind in (KIND_UPSTREAM, KIND_MALFORMED):
            return _error(502, "upstream_error", "The mesh service failed to produce a model.", request_id=request_id)
        return _error(502, "upstream_error", "The mesh service failed.", request_id=request_id)


async def registry_snapshot(deps: MeshApiDeps) -> MeshApiDeps:
    """deps whose registry readers return one snapshot, read off the event loop. The readers query the admin store, so
    a read on the loop stalls every request; one read also keeps the route row, the service and the observer from
    different registry versions. The probe cache is shared, not copied."""
    frontends, services = await asyncio.to_thread(lambda: (deps.frontend_records(), deps.services()))
    return replace(deps, frontend_records=lambda: frontends, services=lambda: services)


def _policy_refusal(decision: Any, request_id: str) -> JSONResponse:
    """403 in the OpenAI error shape, as the other Try workflows refuse a plan denial."""
    reason = decision.reasons[0] if decision.reasons else "This isn't in your access plan."
    return _error(403, decision.code or "policy_denied", reason, request_id=request_id)


def _observer_url(deps: MeshApiDeps, spec: ExclusivePoolSpec) -> str:
    config = _config_of(deps.services().get(spec.observer_service))
    base = service_base_url(config)
    return base + spec.observer_path


def _observer_problem(deps: MeshApiDeps, spec: ExclusivePoolSpec) -> tuple[str | None, str]:
    """Whether the observer service may be asked at all, and the URL to ask. Reads no capacity."""
    record = deps.services().get(spec.observer_service)
    if not isinstance(record, Mapping) or record.get("enabled") is False:
        return "observer service is disabled or absent", ""
    url = _observer_url(deps, spec)
    if not url.startswith(("http://", "https://")):
        return "observer service is not available", ""
    return None, url


async def discovery_capability(deps: MeshApiDeps) -> Capability:
    """The mesh row for the capability listing. Its facts come from the route's own frontend row and the service
    record; its state comes from one live probe of the service health and the observer's facts. It acquires no
    capacity and dispatches nothing, and it reports an unavailable row, never an error, when a fact is missing."""
    manifest = deps.manifest
    deps = await registry_snapshot(deps)
    row, problem = route_row(deps.frontend_records(), manifest)
    tiers = (row or {}).get("runtime_policy") or {}
    spec: ExclusivePoolSpec | None = None
    service: Mapping[str, Any] | None = None
    if problem is None:
        problem, spec, service = service_problem(deps.services(), manifest)
    if problem is None and spec is not None and service is not None:
        problem = await _pool_problem(deps, spec, service)
    available = problem is None
    projection = AvailabilityProjection(
        service_id=manifest.service_id,
        enabled=bool(service is not None and service.get("enabled") is not False),
        configured=service is not None,
        up=(available if service is not None else None),
        status="ready" if available else "unavailable",
        reason="Service health and observer facts answered." if available else str(problem),
        measured=service is not None and spec is not None,
        evidence={"kind": "live_probe"},
    )
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
        lifecycle="warm" if available else "stopped",
        health="healthy" if available else "unhealthy",
        resources=ResourceEstimate(gpu_required=True),
        caveats=() if available else (problem,),
        availability=projection.to_public_dict(),
    )


async def _pool_problem(deps: MeshApiDeps, spec: ExclusivePoolSpec, service: Mapping[str, Any]) -> str | None:
    """The listing's answer for the pool, from the probe cache. A pair never probed waits for its first probe, which
    every listing that arrives meanwhile shares. A cached answer returns at once; once it is older than
    POOL_PROBE_FRESH_SECONDS, one refresh starts behind it. A refresh that is already in flight is not doubled."""
    config = _config_of(service)
    base = service_base_url(config)
    if not base:
        return "service has no base URL"
    observer_problem, observer_url = _observer_problem(deps, spec)
    if observer_problem is not None:
        return observer_problem
    key = (base + str(config.get("health_endpoint") or "/health"), observer_url)
    answer = deps.probe_cache.answers.get(key)
    if answer is None:
        return await asyncio.shield(_pool_probe_task(deps, spec, service, key))
    finished, reason = answer
    if time.monotonic() - finished >= POOL_PROBE_FRESH_SECONDS:
        _pool_probe_task(deps, spec, service, key)
    return reason


def _pool_probe_task(deps: MeshApiDeps, spec: ExclusivePoolSpec, service: Mapping[str, Any],
                     key: tuple[str, str]) -> asyncio.Task:
    """The probe in flight for this pair, or a new one. Shielded by its callers, so a cancelled listing never cancels it."""
    task = deps.probe_cache.inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(_run_pool_probe(deps, spec, service, key))
        deps.probe_cache.inflight[key] = task
    return task


async def _run_pool_probe(deps: MeshApiDeps, spec: ExclusivePoolSpec, service: Mapping[str, Any],
                          key: tuple[str, str]) -> str | None:
    """Probe the pool once and record the answer. Never raises: a probe that fails is a reason on the row."""
    try:
        try:
            reason = await _probe_pool(deps, spec, service)
        except Exception as error:  # noqa: BLE001 - the row carries one bounded reason, never the raw error
            reason = f"probe failed: {type(error).__name__}"
        deps.probe_cache.answers[key] = (time.monotonic(), reason)
        return reason
    finally:
        deps.probe_cache.inflight.pop(key, None)


async def _probe_pool(deps: MeshApiDeps, spec: ExclusivePoolSpec, service: Mapping[str, Any]) -> str | None:
    """The reason the pool cannot take work now, or None. One health GET and one observer GET; nothing is leased."""
    config = _config_of(service)
    base = service_base_url(config)
    if not base:
        return "service has no base URL"
    observer_problem, observer_url = _observer_problem(deps, spec)
    if observer_problem is not None:
        return observer_problem
    async with deps.client_factory() as client:
        health_url = base + str(config.get("health_endpoint") or "/health")
        try:
            health = await client.get(health_url, timeout=_HEALTH_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - any failure to ask is one bounded reason, never the raw error
            return "service health did not answer"
        if not 200 <= health.status_code < 300:
            return "service health did not answer with 2xx"
        try:
            observed = await client.get(observer_url, timeout=spec.timeout_seconds)
        except Exception:  # noqa: BLE001
            return "observer did not answer"
        if observed.status_code != 200:
            return "observer did not answer with 200"
        try:
            document = observed.json()
        except ValueError:
            return "observer answer is not JSON"
    try:
        observation_from_document(document, spec)
    except PoolObserverError as error:
        return f"observer facts are not usable: {error}"
    return None


def _split_form(form: Any, manifest: MeshManifest) -> tuple[dict[str, str], UploadFile | None]:
    """Separate the single image part from the text fields. Any other file part is refused."""
    supplied: dict[str, str] = {}
    image: UploadFile | None = None
    for key, value in form.multi_items():
        if key == manifest.image_input:
            if not isinstance(value, UploadFile) or image is not None:
                raise MeshInputError("invalid_image", "send exactly one image file part", param=key)
            image = value
            continue
        if isinstance(value, UploadFile):
            raise MeshInputError("unknown_field", f"{key} is not a field of this route", param=key)
        if key in supplied:
            raise MeshInputError("duplicate_field", f"{key} is sent more than once", param=key)
        supplied[key] = str(value)
    return supplied, image


async def _watch_disconnect(request: Request) -> None:
    """Read the channel until the client disconnects. Starts only after the body is fully consumed."""
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return

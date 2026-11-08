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
fully read. The engine has no cancel call, so a client that goes away does not free the pool: the HTTP task
keeps the lease until the engine answers or times out (a second run would meet a busy engine), its answer is
dropped, and the receipt owner records the cancellation at once. A run that finds the lease held is refused at once with
503 and Retry-After, in the sync and the async form alike; the engine call has a wall-clock bound (the service's timeout).

``Prefer: respond-async`` (RFC 7240) runs the same steps as a media job. Every check above still runs first,
and the lease is taken before the answer, so a refusal is the same synchronous 4xx/503. After that the answer
is 202 with the job, and a gateway task that no client connection owns holds the lease, posts to the engine,
keeps the GLB in the Library, attaches it to the job and closes the one receipt. A cancel of a running job
waits for the engine to answer, like a disconnect. A job left running by a stopped gateway is failed at the
next start by the media-job recovery (``receipt_store.close_orphan`` closes its receipt).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from gateway import engine_egress
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
    MeshManifestError,
    bind_request,
    downstream_form,
    sniff_image,
)
from gateway import asset_types, client_errors, frontend_profiles, log_scrub, media_jobs, operation_security, receipt_store, request_history_capture
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
# The media job's mode: what media-job recovery and the outstanding-job count key on.
JOB_MODE = "async_mesh"
PREFER_ASYNC = "respond-async"
# The receipt's terminal reason for a job a stopped gateway left, whether it noticed (a stop) or not (a crash: the next
# start's media-job recovery closes the receipt).
GATEWAY_STOPPED = receipt_store.GATEWAY_RESTARTED
JOB_STOPPED = "The gateway stopped before the model was finished; make it again."
CANCELED_BY_REQUESTER = "Canceled by requester"
JOB_NOT_KEPT = "The model was made but could not be saved; make it again."
_HEALTH_TIMEOUT_SECONDS = 5.0
# An answer of the availability probe younger than this is served as it is. An older one is still served, while one refresh
# runs behind it, so a slow or hung trellis host delays only the first listing.
POOL_PROBE_FRESH_SECONDS = 10.0
FRONTEND_ABSENT = "frontend record is absent"
FRONTEND_DISABLED = "frontend record is disabled"
SERVICE_ABSENT = "service is absent"
SERVICE_DISABLED = "service is disabled"
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


_MESH_ADAPTER = "http-multipart"


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


_ROUTE_TYPES = frozenset({"asset", "integer", "string", "object"})
# The picture types the route sniffs (``sniff_image``). The picture input must accept exactly these: a type the route cannot
# read is never advertised, and a type it reads is never refused by the contract.
_SNIFFED_PICTURES = frozenset({"image/png", "image/jpeg", "image/webp"})
# The wire reads an integer below this (``mesh_manifest``): a contract bound, default or allowed value past it is refused.
_INTEGER_LIMIT = 2**63
# Constraints of the schema that the route's form decoding does not enforce. A contract that states one is refused, never
# served with the rule silently dropped.
_UNENFORCED_KEYS = ("step", "offset", "generate", "min_length", "max_length")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _picture_accepts_exactly(accepts: Any) -> bool:
    return isinstance(accepts, list) and all(isinstance(item, str) for item in accepts) \
        and {item.strip().lower() for item in accepts} == _SNIFFED_PICTURES


def _input_problems(name: Any, spec: Any, mapped: bool) -> list[str]:
    """What the route cannot do with one input: a spec that is not a mapping, a type it cannot read, an object that is mapped
    to a form field, a constraint its decoding does not enforce, a number past what the wire reads, a required option."""
    if not isinstance(name, str) or not name:
        return [f"input name {name!r} is not text"]
    if not isinstance(spec, Mapping):
        return [f"input {name} must be a mapping"]
    kind = spec.get("type")
    if not isinstance(kind, str) or kind not in _ROUTE_TYPES:
        return [f"input {name} has a type the route cannot read"]
    if kind == "asset":
        return []  # the picture's acceptance is checked by the profile, for the one required picture
    problems: list[str] = []
    if spec.get("required") is True:
        problems.append(f"input {name} cannot be required: only the picture is")
    if kind == "object" and mapped:
        problems.append(f"input {name} is an object and cannot be mapped to a form field")
    for key in _UNENFORCED_KEYS:
        if key in spec:
            problems.append(f"input {name} states {key}, which the route does not enforce")
    if kind == "integer":
        for key in ("minimum", "maximum", "default"):
            if spec.get(key) is not None and not _is_int(spec[key]):
                problems.append(f"input {name} {key} must be an integer")
        numbers = [spec.get(key) for key in ("minimum", "maximum", "default")]
        numbers += spec["enum"] if isinstance(spec.get("enum"), list) else []
        if any(_is_int(number) and abs(number) >= _INTEGER_LIMIT for number in numbers):
            problems.append(f"input {name} holds a number past what the route reads")
    if kind == "string" and "default" in spec and spec["default"] is not None and not isinstance(spec["default"], str):
        problems.append(f"input {name} default must be text")
    enum = spec.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        problems.append(f"input {name} enum must be a non-empty list")
    return problems


def _output_count_problem(spec: Any) -> bool:
    """Whether a stated output count allows other than one body."""
    count = spec.get("count") if isinstance(spec, Mapping) else None
    if count is None:
        return False
    return not isinstance(count, Mapping) or count.get("max") != 1 or (count.get("min") not in (None, 0, 1))


def mesh_profile_errors(contract: Mapping[str, Any]) -> list[str]:
    """Why the mesh frontend cannot serve this workflow, from the contract only: the wire carries one file part and calls
    one engine over one multipart POST, and answers with one GLB body. Empty when it can. A contract of any shape gets an
    answer here, never an exception."""
    if not isinstance(contract, Mapping):
        return ["the contract must be a mapping"]
    problems: list[str] = []
    if contract.get("intent") != MESH_INTENT:
        problems.append(f"intent must be {MESH_INTENT!r}")
    for key in ("id", "version"):
        value = contract.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{key} must be a non-empty string")
        elif value != value.strip():
            problems.append(f"{key} must not start or end with a space")
    raw_inputs = contract.get("inputs")
    inputs = raw_inputs if isinstance(raw_inputs, Mapping) else {}
    if not inputs:
        problems.append("inputs must be a non-empty mapping")
    pictures = [name for name, spec in inputs.items() if isinstance(spec, Mapping) and spec.get("type") == "asset"]
    required = [name for name in pictures if inputs[name].get("required") is True]
    if len(pictures) != 1 or len(required) != 1:
        problems.append("exactly one required picture input")
    bindings = contract.get("bindings") if isinstance(contract.get("bindings"), Mapping) else {}
    if len(bindings) != 1:
        problems.append("exactly one binding")
    binding = next(iter(bindings.values()), None) if len(bindings) == 1 else None
    if len(bindings) == 1 and (next(iter(bindings)) != _MESH_ADAPTER or not isinstance(binding, Mapping)
                               or not isinstance(binding.get("service"), str) or not isinstance(binding.get("path"), str)
                               or not isinstance(binding.get("map"), Mapping)):
        problems.append(f"the binding must be a {_MESH_ADAPTER} binding with a service, a path and a map")
    mapping = binding.get("map") if isinstance(binding, Mapping) and isinstance(binding.get("map"), Mapping) else {}
    for name, spec in inputs.items():
        problems.extend(_input_problems(name, spec, name in mapping))
    for logical, target in mapping.items():
        if logical not in inputs:
            problems.append(f"map names {logical!r}, which is not an input")
        if not isinstance(target, str) or not target:
            problems.append(f"map target for {logical} must be a form field name")
    if len(bindings) == 1 and len(required) == 1 and required[0] not in mapping:
        problems.append("the picture input must be mapped")
    if len(required) == 1 and not _picture_accepts_exactly(inputs[required[0]].get("accepts")):
        problems.append(f"the picture input must accept {', '.join(sorted(_SNIFFED_PICTURES))} and nothing else")
    outputs = contract.get("outputs") if isinstance(contract.get("outputs"), Mapping) else {}
    media = [spec.get("media_type") for spec in outputs.values() if isinstance(spec, Mapping)]
    if len(outputs) != 1 or media != [MESH_MEDIA_TYPE]:
        problems.append(f"exactly one output, {MESH_MEDIA_TYPE}")
    elif _output_count_problem(next(iter(outputs.values()))):
        problems.append("the output must be one model")
    return problems


frontend_profiles.register(MESH_INTENT, mesh_profile_errors)


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


@dataclass(frozen=True)
class MeshApiDeps:
    """Everything the handler reads from the gateway. Tests inject fakes for each of them."""

    # Zero-argument: the workflow record as it is now. Each request and each listing calls it once and uses that one
    # value throughout, so a refresh in the middle cannot mix two records.
    current_manifest: Callable[[], MeshWorkflow]
    # Synchronous: the gateway's authenticator. Returns the caller's context, or the refusal to send.
    authenticate: Callable[[Request, str], Any]
    # The normalized frontend records (live registry, else seed).
    frontend_records: Callable[[], list[Mapping[str, Any]]]
    # Service ID -> normalized service record (live registry).
    services: Callable[[], Mapping[str, Mapping[str, Any]]]
    # open_receipt(auth, request_id, path, manifest) -> an async context manager with report(status=, reason=). It
    # names the record this request read (``current_manifest``), so the receipt cannot name a later one.
    open_receipt: Callable[[Any, str, str, Any], Any]
    leases: ExclusivePoolLeaseManager
    # A fresh client per request, closed when the request's work ends or is cancelled.
    client_factory: Callable[[], httpx.AsyncClient]
    new_request_id: Callable[[], str]
    # route_policy(auth, frontend_row, manifest) -> a policy decision for this caller on this route and this request's
    # record (the same evaluator the other Try workflows use). A decision with ``denied`` true refuses the request with
    # 403 before the body is read.
    route_policy: Callable[[Any, Mapping[str, Any], Any], Any]
    # Answers of the availability probe only (pool_evidence); the request path always probes live.
    probe_cache: PoolProbeCache = field(default_factory=PoolProbeCache)
    # store_output(auth, output) -> the library asset id the finished model was stored under, or None to store nothing.
    # ``output`` is a WorkflowOutput: the model, the workflow's display name, the request id and the picture it was made
    # from. A storage fault never fails a finished model: the caller still gets the bytes, without an asset id.
    store_output: Callable[[Any, asset_types.WorkflowOutput], str | None] | None = None
    # The media-job side of ``Prefer: respond-async``. None (or no store_output) answers every request synchronously.
    jobs: "MeshJobDeps | None" = None
    # register_job_counter(name, count): how the gateway learns of work that outlives its request, so a safe restart
    # waits for it. The frontend registers the count of its own running work (async jobs, and sync runs whose client left).
    register_job_counter: Callable[[str, Callable[[], int]], None] | None = None
    # discard_output(asset_id): remove a stored model from the Library (one a canceled job must not keep). None keeps it.
    discard_output: Callable[[str], object] | None = None
    # egress_refusal(service_id) -> a Response that refuses the send, or None; called just before the request is sent to the
    # engine (never for the health read). The gateway uses it to keep an unleased path out of a transition-enabled domain.
    egress_refusal: Callable[[str], Response | None] | None = None
    # workflow_available() -> whether the live registry holds the route's workflow record now. A route whose record is
    # absent or disabled answers unavailable; it is a state of the route, never a failure to start. None means always.
    workflow_available: Callable[[], bool] | None = None


@dataclass(frozen=True)
class MeshJobDeps:
    """What the async form takes from the gateway besides the shared media-job store."""

    # The media-job store's database (read on each use, so a test or a move of the store is followed).
    path: Callable[[], Path]
    # owner(auth) -> the job's caller fields: app_profile, api_key_name, app_id, owner_key_hash.
    owner: Callable[[Any], Mapping[str, Any]]
    # response(job, request) -> the 202 answer the other media-job submits return (body, Location).
    response: Callable[[Mapping[str, Any], Request], Response]


@dataclass(frozen=True)
class _Failure:
    """One failed run, as the sync route answers it and as the job records it."""

    reason: str  # the receipt's terminal reason
    status: int
    code: str
    message: str
    category: str  # media_jobs.ERROR_CATEGORIES
    retry_after: bool = False


def _failure_of(error: BaseException) -> _Failure:
    """Map what a run raised to its bounded outcome. The detail stays in the log."""
    if isinstance(error, PoolCapacityError):
        LOG.info("mesh capacity refused: %s", error.status.value)
        return _Failure("capacity", 503, "resource_unavailable", "The GPU is busy. Try again shortly.", "capacity", True)
    if isinstance(error, PoolObserverError):
        return _Failure("observer", 503, "resource_unavailable", "The GPU is busy. Try again shortly.", "capacity", True)
    if isinstance(error, MultipartFailure):
        LOG.warning("mesh downstream failed: %s", error)
        if error.kind == KIND_TIMEOUT:
            return _Failure(error.kind, 504, "upstream_timeout", "The mesh service did not answer in time.", "timeout")
        if error.kind == KIND_CONNECT:
            return _Failure(error.kind, 502, "upstream_unavailable", "The mesh service could not be reached.", "upstream")
        if error.kind in (KIND_UPSTREAM, KIND_MALFORMED):
            return _Failure(error.kind, 502, "upstream_error", "The mesh service failed to produce a model.", "upstream")
        return _Failure(error.kind, 502, "upstream_error", "The mesh service failed.", "upstream")
    LOG.error("mesh route fault", exc_info=error)
    return _Failure("internal", 500, "internal_error", "The mesh route failed.", "internal")


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


def route_row(records: Sequence[Mapping[str, Any]], manifest: MeshWorkflow) -> tuple[Mapping[str, Any] | None, str | None]:
    """The frontend row for this route and why it is not usable, or (None, reason). Reads no default."""
    record = next((item for item in records if item.get("id") == FRONTEND_ID), None)
    if record is None:
        return None, FRONTEND_ABSENT
    if record.get("enabled") is not True:
        return None, FRONTEND_DISABLED
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


def service_problem(services: Mapping[str, Mapping[str, Any]], manifest: MeshWorkflow) -> tuple[str | None, ExclusivePoolSpec | None, Mapping[str, Any] | None]:
    """The selected service's policy, before the body is read: (problem, exclusive pool spec, service)."""
    service = services.get(manifest.service_id)
    if not isinstance(service, Mapping):
        return SERVICE_ABSENT, None, None
    if service.get("enabled") is False:
        return SERVICE_DISABLED, None, service
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


def prefers_async(value: str | None) -> bool:
    """Whether a ``Prefer`` header asks for ``respond-async`` (RFC 7240: comma-separated tokens, optional parameters)."""
    for preference in (value or "").split(","):
        if preference.split(";", 1)[0].split("=", 1)[0].strip().lower() == PREFER_ASYNC:
            return True
    return False


def register_mesh_routes(app: FastAPI, deps: MeshApiDeps) -> None:
    # Runs that no client connection owns: the async jobs, and the sync runs whose client left. Held here so the event
    # loop never drops them.
    runs: set[asyncio.Future] = set()

    def keep(task: asyncio.Future) -> None:
        runs.add(task)
        task.add_done_callback(runs.discard)

    if deps.register_job_counter is not None:
        deps.register_job_counter(FRONTEND_ID, lambda: len(runs))

    @app.post(MESH_PATH, openapi_extra=operation_security.openapi_extra(operation_security.APP_KEY))
    async def generate_mesh(request: Request) -> Response:
        request_id = deps.new_request_id()
        gate = await asyncio.to_thread(deps.authenticate, request, MESH_PATH)
        if isinstance(gate, Response):
            return gate
        if deps.workflow_available is not None and not deps.workflow_available():
            LOG.warning("mesh route refused: %s", WORKFLOW_ABSENT)
            return _unavailable(request_id)
        try:
            manifest = deps.current_manifest()  # once: this request uses this record from here to its end
        except MeshManifestError as error:  # the record no longer fits the mesh wire: the route is down, not failing
            LOG.warning("mesh route refused: %s", error)
            return _unavailable(request_id)
        facts = await registry_snapshot(deps)
        row, record_problem = route_row(facts.frontend_records(), manifest)
        if row is None:
            LOG.warning("mesh route refused: %s", record_problem)
            return _unavailable(request_id)
        problem, spec, service = service_problem(facts.services(), manifest)
        if problem is not None or spec is None or service is None:
            LOG.warning("mesh route refused: %s", problem)
            return _unavailable(request_id)
        decision = deps.route_policy(gate, row, manifest)
        if decision is not None and decision.denied:
            return _policy_refusal(decision, request_id)
        observer = _observer_problem(facts, spec)
        # What this route runs, for the kept request: it has no model in its body, so History reads the title from here.
        run = {"workflow": manifest.id, "title": manifest.data.get("display_name"), "capability": CAPABILITY}
        setattr(request.state, request_history_capture.RUN_STATE_KEY, run)
        # The async form needs the media-job store and a Library to keep the model in; without both, it is the sync form.
        wants_async = (deps.jobs is not None and deps.store_output is not None
                       and prefers_async(request.headers.get("prefer")))
        async with deps.open_receipt(gate, request_id, MESH_PATH, manifest) as owner:
            parsed = await _read_request(request, request_id, owner, manifest)
            if isinstance(parsed, Response):
                return parsed
            fields, image_asset, source_name = parsed
            if wants_async:
                return await _submit_job(request, request_id, owner, service, spec, observer, fields, image_asset, gate,
                                         source_name, manifest)
            return await _watch_and_run(request, request_id, owner, service, spec, observer, fields, image_asset, gate,
                                        (run, source_name), manifest)

    async def _read_request(request: Request, request_id: str, owner: Any,
                            manifest: MeshWorkflow) -> tuple[Mapping[str, str], Any, str] | Response:
        """The engine's form fields, its image and the name the caller gave the picture, or the 400 for a body that does
        not bind to the manifest."""
        try:
            form = await read_form(request, max_files=2, max_fields=len(manifest.inputs) + 2,
                                   max_part_size=MAX_IMAGE_BYTES + 1)
        except Exception:  # noqa: BLE001 - any malformed or oversized multipart is one 400
            owner.report(status="failed", reason="invalid_multipart")
            return _error(400, "invalid_multipart", "The request body is not valid multipart form data.",
                          request_id=request_id)
        try:
            supplied, image_part = _split_form(form, manifest)
            image = sniff_image(await image_part.read(MAX_IMAGE_BYTES + 1), max_bytes=manifest.max_image_bytes,
                               param=manifest.image_input) \
                if image_part is not None else None
            mesh_request = bind_request(manifest, supplied, image)
        except MeshInputError as error:
            owner.report(status="failed", reason=error.code)
            return _error(400, error.code, str(error), param=error.param, request_id=request_id)
        finally:
            await form.close()
        fields, image_asset = downstream_form(manifest, mesh_request)
        # The name the caller gave the picture. The service is sent a neutral name; the library keeps this one.
        source_name = str(getattr(image_part, "filename", "") or image_asset.filename)
        return fields, image_asset, source_name

    async def _healthy(client: httpx.AsyncClient, base: str, config: Mapping[str, Any]) -> bool:
        health_url = base + str(config.get("health_endpoint") or "/health")
        try:
            healthy = await client.get(health_url, timeout=_HEALTH_TIMEOUT_SECONDS, extensions=engine_egress.probe())
        except httpx.HTTPError:
            return False
        return 200 <= healthy.status_code < 300

    async def _watch_and_run(request: Request, request_id: str, owner: Any, service: Mapping[str, Any],
                             spec: ExclusivePoolSpec, observer: tuple[str | None, str], fields: Mapping[str, str],
                             image: Any, gate: Any, run_and_name: tuple[dict[str, Any], str],
                             manifest: MeshWorkflow) -> Response:
        config = _config_of(service)
        base = service_base_url(config)
        if not base:
            owner.report(status="failed", reason="service_unavailable")
            return _unavailable(request_id)
        client = deps.client_factory()
        detached = False
        try:
            if not await _healthy(client, base, config):
                owner.report(status="failed", reason="unhealthy")
                return _unavailable(request_id)

            if deps.egress_refusal is not None:
                refusal = deps.egress_refusal(manifest.service_id)
                if refusal is not None:
                    owner.report(status="failed", reason="not_admitting")
                    return refusal
            work = asyncio.ensure_future(_lease_and_post(client, request_id, base, config, spec, observer, fields,
                                                         image, manifest))
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
                return await _finish(work, owner, request_id, spec, gate, image, *run_and_name, manifest)
            # The client went away first. The engine cannot be told to stop, so the HTTP task is not cancelled: it
            # keeps the lease until the engine answers or times out (a second run would meet a busy engine), and its
            # answer is dropped. Only the receipt closes now, as cancelled.
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            detached = True
            keep(asyncio.ensure_future(_drain(work, client)))
            raise asyncio.CancelledError()
        finally:
            if not detached:
                await client.aclose()

    async def _drain(work: asyncio.Future, client: httpx.AsyncClient) -> None:
        """Let a run whose client left end on its own: wait for the engine, drop the answer, close the client."""
        try:
            await asyncio.gather(work, return_exceptions=True)
        finally:
            await client.aclose()

    async def _lease_and_post(client: httpx.AsyncClient, request_id: str, base: str, config: Mapping[str, Any],
                              spec: ExclusivePoolSpec, observer: tuple[str | None, str], fields: Mapping[str, str],
                              image: Any, manifest: MeshWorkflow,
                              admitted: Callable[[], None] | None = None) -> bytes:
        """One lease, held from the observation until the engine answers. ``admitted`` runs when the lease is held,
        before the engine is called. A pool whose lease is held (by a run still going, even one whose caller left) is
        refused at once, never queued: the sync and the async form answer a busy engine the same way."""
        observer_problem, observer_url = observer

        async def observe() -> Any:
            if observer_problem is not None:
                raise PoolObserverError(observer_problem)
            try:
                response = await client.get(observer_url, timeout=spec.timeout_seconds, extensions=engine_egress.probe())
            except httpx.HTTPError as error:
                raise PoolObserverError("observer did not answer") from error
            if response.status_code != 200:
                raise PoolObserverError("observer did not answer")
            try:
                return response.json()
            except ValueError as error:
                raise PoolObserverError("observer answer is not JSON") from error

        async with deps.leases.lease(spec, request_id=request_id, observe=observe, wait=False):
            if admitted is not None:
                admitted()
            timeout = float(config.get("timeout") or 900)
            try:
                # The client's timeout bounds each read, so an engine that sends a byte now and then would hold the lease
                # without end; this bounds the whole call.
                async with asyncio.timeout(timeout):
                    return await post_multipart(
                        client,
                        base + manifest.transport_path,
                        image_field=manifest.mapping[manifest.image_input],
                        image=image,
                        fields=fields,
                        timeout_seconds=timeout,
                        max_response_bytes=MAX_GLB_BYTES,
                    )
            except TimeoutError as error:
                raise MultipartFailure(KIND_TIMEOUT) from error

    def _workflow_output(glb: bytes, request_id: str, image: Any, source_name: str,
                         manifest: MeshWorkflow) -> asset_types.WorkflowOutput:
        """The model with what made it: the workflow's name, the request and the picture it came from."""
        return asset_types.WorkflowOutput(
            data=glb, suffix=".glb", workflow_name=manifest.data.get("display_name"), request_id=request_id,
            inputs=(asset_types.OutputInput(image.data, source_name, image.content_type),),
            takes_text=manifest.takes_text)

    async def _finish(work: asyncio.Future, owner: Any, request_id: str, spec: ExclusivePoolSpec, gate: Any,
                      image: Any, run: dict[str, Any], source_name: str,
                      manifest: MeshWorkflow) -> Response:
        try:
            glb = work.result()
        except Exception as error:  # noqa: BLE001 - every fault is one bounded outcome (_failure_of)
            failure = _failure_of(error)
            owner.report(status="failed", reason=failure.reason)
            return _failure_response(failure, spec, request_id)
        owner.report(status="succeeded", reason="succeeded")
        headers = {"x-request-id": request_id}
        if deps.store_output is not None:
            try:
                asset_id = await asyncio.to_thread(deps.store_output, gate,
                                                   _workflow_output(glb, request_id, image, source_name, manifest))
            except Exception:  # noqa: BLE001 - a finished model is still returned when the library cannot keep it
                LOG.exception("mesh output was not stored")
                asset_id = None
            if asset_id:
                headers["x-asset-id"] = asset_id
                # The kept request says which library item this run made, so History can open it.
                run["asset_id"] = asset_id
        return Response(content=glb, media_type=MESH_MEDIA_TYPE, headers=headers)

    def _failure_response(failure: _Failure, spec: ExclusivePoolSpec, request_id: str) -> Response:
        return _error(failure.status, failure.code, failure.message,
                      headers=_retry_after_headers(spec) if failure.retry_after else None, request_id=request_id)

    # --- Prefer: respond-async ------------------------------------------------------------------------------------

    async def _submit_job(request: Request, request_id: str, owner: Any, service: Mapping[str, Any],
                          spec: ExclusivePoolSpec, observer: tuple[str | None, str], fields: Mapping[str, str],
                          image: Any, gate: Any, source_name: str, manifest: MeshWorkflow) -> Response:
        """Take the lease, then answer 202 with the job. From there the job's task owns the lease, the engine call, the
        Library write and the receipt; this request's connection owns none of them."""
        jobs = deps.jobs
        assert jobs is not None
        config = _config_of(service)
        base = service_base_url(config)
        if not base:
            owner.report(status="failed", reason="service_unavailable")
            return _unavailable(request_id)
        client = deps.client_factory()
        try:
            healthy = await _healthy(client, base, config)
        except BaseException:  # a refusal the gateway's error handler formats still leaves no open client
            await client.aclose()
            raise
        if not healthy:
            await client.aclose()
            owner.report(status="failed", reason="unhealthy")
            return _unavailable(request_id)
        # The same send-time check as the sync route, before the job exists: a refused send leaves no job behind.
        if deps.egress_refusal is not None:
            refusal = deps.egress_refusal(manifest.service_id)
            if refusal is not None:
                await client.aclose()
                owner.report(status="failed", reason="not_admitting")
                return refusal
        path = jobs.path()
        job = media_jobs.MediaJob(
            intent=MESH_INTENT, workflow=manifest.id, backend=manifest.service_id, request_id=request_id,
            status=media_jobs.STATUS_RUNNING, progress=5, metadata={"mode": JOB_MODE}, **jobs.owner(gate))
        held: asyncio.Future = asyncio.get_running_loop().create_future()
        # The task closes the client. It answers ``held`` when the lease is taken (the job then exists) or refused.
        task = asyncio.ensure_future(_run_job(job, path, owner, client, held, request_id, base, config, spec, observer,
                                              fields, image, gate, source_name, manifest))
        keep(task)
        try:
            refusal = await asyncio.shield(held)
        except asyncio.CancelledError:
            if not held.done():
                task.cancel()  # the client left before anything was admitted; nothing is left to finish
            raise  # an admitted job goes on without this connection (the task has taken over the receipt)
        if refusal is not None:  # a refusal is the sync route's own answer
            await asyncio.gather(task, return_exceptions=True)  # the receipt is closed by then
            return _failure_response(refusal, spec, request_id)
        stored = media_jobs.get(job.job_id, audience="internal", path=path) or job.to_dict("internal")
        response = jobs.response(stored, request)
        response.headers["Preference-Applied"] = PREFER_ASYNC
        response.headers["x-request-id"] = request_id
        return response

    async def _run_job(job: media_jobs.MediaJob, path: Path, owner: Any, client: httpx.AsyncClient,
                       held: asyncio.Future, request_id: str, base: str, config: Mapping[str, Any],
                       spec: ExclusivePoolSpec, observer: tuple[str | None, str], fields: Mapping[str, str],
                       image: Any, gate: Any, source_name: str,
                       manifest: MeshWorkflow) -> None:
        """The whole life of one async run. It ends with the job finished, the lease freed and the receipt closed."""
        job_id = job.job_id
        made = False

        def admitted() -> None:
            nonlocal made
            media_jobs.create(job, path=path)  # with the lease held, so a refused run leaves no job behind
            made = True
            owner.handoff()  # from here this task closes the receipt, whatever becomes of the request that submitted it
            held.set_result(None)
            _event(job_id, path, "started", media_jobs.STATUS_RUNNING, job.progress)

        def refuse(failure: _Failure) -> None:
            if not held.done():
                held.set_result(failure)

        failure_of: BaseException | None = None
        try:
            try:
                try:
                    glb = await _lease_and_post(client, request_id, base, config, spec, observer, fields, image, manifest,
                                                admitted=admitted)
                finally:
                    await client.aclose()
            except Exception as error:  # noqa: BLE001
                failure = _failure_of(error)
                if not made:
                    owner.report(status="failed", reason=failure.reason)
                    refuse(failure)  # refused before the job existed: the submit answers it
                    return
                if _cancel_requested(job_id, path):  # the requester stopped it first: job and receipt both say canceled
                    owner.report(status="cancelled", reason="cancelled")
                    _settle(job_id, path, media_jobs.STATUS_CANCELED, CANCELED_BY_REQUESTER)
                else:
                    owner.report(status="failed", reason=failure.reason)
                    _settle(job_id, path, media_jobs.STATUS_FAILED, failure.message, info={
                        "error_code": failure.code, "error_category": failure.category,
                        "retryable": failure.category != "internal", "upstream_http_status": None})
                return
            await _keep_result(job_id, path, owner, gate, _workflow_output(glb, request_id, image, source_name, manifest))
        except BaseException as error:  # noqa: BLE001 - a stop of the gateway must still end the job
            stopped = isinstance(error, asyncio.CancelledError)
            # A stop fails the job (it must be made again), so the receipt fails with it, as the next start's recovery
            # would close it; settle is then given no exception, which would close it as cancelled.
            failure_of = None if stopped and made else error
            if made:
                if stopped:
                    owner.report(status="failed", reason=GATEWAY_STOPPED)
                _settle(job_id, path, media_jobs.STATUS_FAILED, JOB_STOPPED if stopped else "The mesh route failed.")
            refuse(_Failure("internal", 500, "internal_error", "The mesh route failed.", "internal"))
            if not isinstance(error, Exception):
                raise
            LOG.error("mesh job fault: %s", log_scrub.describe_error(error))
        finally:
            if made:  # a refused run has no job; the request that submitted it closes its receipt
                await owner.settle(failure_of)

    async def _discard(asset_id: str) -> None:
        """Take a model the job no longer owns out of the Library. Best effort: a model left behind is only unlisted by the job."""
        if deps.discard_output is None:
            return
        try:
            await asyncio.to_thread(deps.discard_output, asset_id)
        except Exception:  # noqa: BLE001
            LOG.warning("mesh output of a canceled job was not removed", exc_info=True)

    async def _keep_result(job_id: str, path: Path, owner: Any, gate: Any, output: asset_types.WorkflowOutput) -> None:
        """The engine answered: keep the model in the Library and attach it to the job, unless the job was canceled."""
        if _cancel_requested(job_id, path):
            owner.report(status="cancelled", reason="cancelled")
            _settle(job_id, path, media_jobs.STATUS_CANCELED, CANCELED_BY_REQUESTER)
            return
        try:
            asset_id = await asyncio.to_thread(deps.store_output, gate, output)  # type: ignore[misc]
        except Exception:  # noqa: BLE001
            LOG.exception("mesh output was not stored")
            asset_id = None
        if not asset_id:
            owner.report(status="failed", reason="store_failed")
            _settle(job_id, path, media_jobs.STATUS_FAILED, JOB_NOT_KEPT, info={
                "error_code": "store_failed", "error_category": "internal", "retryable": True,
                "upstream_http_status": None})
            return
        try:
            media_jobs.update_status(
                job_id, media_jobs.STATUS_SUCCEEDED, progress=100,
                add_assets=[{"asset_id": asset_id, "media_type": MESH_MEDIA_TYPE}],
                metadata_patch={"completed_at": int(time.time())}, path=path)
        except media_jobs.CancellationPending:
            # The cancel landed while the model was being written: it wins, and the model is taken out of the Library.
            await _discard(asset_id)
            owner.report(status="cancelled", reason="cancelled")
            _settle(job_id, path, media_jobs.STATUS_CANCELED, CANCELED_BY_REQUESTER)
            return
        owner.report(status="succeeded", reason="succeeded")
        _event(job_id, path, "succeeded", media_jobs.STATUS_SUCCEEDED, 100)


def _cancel_requested(job_id: str, path: Path) -> bool:
    job = media_jobs.get(job_id, path=path)
    return bool(job and (job.get("metadata") or {}).get("cancel_requested_at"))


def _event(job_id: str, path: Path, phase: str, status: str, progress: int) -> None:
    try:
        media_jobs.append_event(job_id, phase, {"phase": phase, "status": status, "progress": progress}, path=path)
    except Exception:  # noqa: BLE001 - an event is a courtesy; the status already says it
        LOG.warning("mesh job event was not written", exc_info=True)


def _settle(job_id: str, path: Path, status: str, message: str, *, info: Mapping[str, Any] | None = None) -> None:
    """Finish the job. A job that is already finished stays as it is; facts the store refuses are left out, so the job
    still ends."""
    for facts in ((info, None) if info is not None else (None,)):
        try:
            media_jobs.update_status(job_id, status, progress=100, error=message, error_info=facts,
                                     metadata_patch={"completed_at": int(time.time())}, path=path)
        except ValueError:
            current = media_jobs.get(job_id, path=path)
            if current is None or media_jobs.is_terminal(current["status"]):
                return
            continue
        except Exception:  # noqa: BLE001
            LOG.exception("mesh job was not finished")
            return
        _event(job_id, path, "canceled" if status == media_jobs.STATUS_CANCELED else "failed", status, 100)
        return


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


def discovery_facts(manifest: MeshWorkflow, frontend_records: Sequence[Mapping[str, Any]]) -> Capability:
    """The mesh row's facts for the shared capability catalog: what the route is, and the tiers its frontend row sets
    (or the defaults, when that row is not usable). It reads nothing and holds no state. The state is the pool's probe,
    which joins as evidence (pool_evidence), so a route whose frontend or service is missing still lists, as Off."""
    row, _ = route_row(frontend_records, manifest)
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
    _, problem = route_row(deps.frontend_records(), manifest)
    if not workflow_present:
        problem = WORKFLOW_ABSENT
    observed_s = int(time.time())
    if problem is None:
        problem, spec, service = service_problem(deps.services(), manifest)
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
    config = _config_of(service)
    base = service_base_url(config)
    if not base:
        return "service has no base URL", int(time.time())
    observer_problem, observer_url = _observer_problem(deps, spec)
    if observer_problem is not None:
        return observer_problem, int(time.time())
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
            reason = await _probe_pool(deps, spec, service)
        except Exception as error:  # noqa: BLE001 - the row carries one bounded reason, never the raw error
            reason = f"probe failed: {type(error).__name__}"
        observed = int(time.time())
        deps.probe_cache.answers[key] = (time.monotonic(), observed, reason)
        return reason, observed
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
            health = await client.get(health_url, timeout=_HEALTH_TIMEOUT_SECONDS, extensions=engine_egress.probe())
        except Exception:  # noqa: BLE001 - any failure to ask is one bounded reason, never the raw error
            return "service health did not answer"
        if not 200 <= health.status_code < 300:
            return "service health did not answer with 2xx"
        try:
            observed = await client.get(observer_url, timeout=spec.timeout_seconds, extensions=engine_egress.probe())
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


def _split_form(form: Any, manifest: MeshWorkflow) -> tuple[dict[str, str], UploadFile | None]:
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

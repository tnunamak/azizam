"""The reviewed runner for a binding that sends one multipart form to a service and reads the finished bytes back.

The run core dispatches here (``workflow_adapters``) for the binding key the protocol description names. Everything the
run needs comes from the job's pin (the contract, the binding) and from ``MultipartHost`` (the gateway's services, its
exclusive-pool leases, its Library). Nothing here names a workflow, an intent or a service.

One run:

1. The service must be present, enabled, and declare an enabled exclusive pool (``binding_problem``, also asked before a
   job exists).
2. The service's health is read. The egress guard is asked just before the send.
3. The durable job pins one admission deadline. Lock contention, insufficient memory
   and missing observations wait in the original FIFO turn, without an execution
   attempt or worker slot. The job projects ``waiting_for_gpu`` then ``gpu_ready``.
   Cancellation withdraws waiting work; deadline expiry returns retryable busy.
   A claim above total capacity is rejected without retry. ``gpu_wait_s: 0`` retains
   immediate refusal with the configured execution retry policy.
4. The lease is held from the observation until the service answers or the wall-clock bound ends. The service has no
   cancel call: a cancel of a running job waits for the answer, and the answer is then dropped.
5. The model is kept in the Library and published as the job's one result.

The attempt marker (``protocol_submitting``) is written just before the send and cleared in the commit that publishes the
result, as for every adapter. A run that meets the marker without a result does not send again.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import time
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

import httpx

from gateway import asset_types, declared_operation_job, engine_egress, job_retry_hint, job_wake, media_jobs, result_bundle
from gateway.engine_protocol.types import AssetRef, AssetsResult, CanonicalRequest, Completed, ResultBundle, ResultContext
from gateway.engine_protocol import description as protocol_description
from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION_V2
from gateway.exclusive_pool import (
    MIB,
    ExclusivePoolLeaseManager,
    ExclusivePoolSpec,
    LeaseWait,
    LeaseWaitStopped,
    PoolCapacityError,
    PoolObserverError,
    observation_from_document,
    parse_resource_capacity,
    physical_resource_ids,
    snapshot_and_claim,
)
from gateway.frontend_shapes.media_job import GPU_BUSY_COPY
from gateway.resource_claims import AdmissionStatus, AdmissionRequest, evaluate_admission
from gateway.gpu_admission import AdmissionScheduler, AdmissionLease, AdmissionExpired, AdmissionWithdrawn
from gateway.http_multipart import (
    KIND_CONNECT,
    KIND_MALFORMED,
    KIND_TIMEOUT,
    KIND_UPSTREAM,
    MultipartFailure,
    post_multipart,
)
from gateway.mesh_manifest import ImageAsset
from gateway.service_registry import service_base_url
from gateway.workflow_run import BINDING_UNAVAILABLE, AdapterIdentity, RunContext, admission_remaining, retry_policy

LOG = logging.getLogger("azizam.multipart")

MAX_ANSWER_BYTES = 512 * MIB
_HEALTH_TIMEOUT_S = 5.0
MAX_PROBE_BODY_BYTES = 1 * MIB  # an observer's JSON answer; a health answer's body is never read
_DEFAULT_TIMEOUT_S = 900.0

SUBMISSION_UNKNOWN = "submission_unknown"
RESOURCE_UNAVAILABLE = "resource_unavailable"
SERVICE_UNAVAILABLE = "service_unavailable"
MODEL_NOT_KEPT = "result_not_kept"
INPUT_UNAVAILABLE = "input_unavailable"
SERVICE_ABSENT = "service is absent"
SERVICE_DISABLED = "service is disabled"


class RunError(Exception):
    """A run that failed. ``code`` and ``category`` are what the job records. The text of the exception never reaches the
    job; only ``client_message`` does, and only an outcome a person can act on sets it."""

    def __init__(self, code: str, category: str, *, retryable: bool = False, client_message: str | None = None,
                 retry_reason: str | None = None, retry_after_s: float | None = None, before_send: bool = False) -> None:
        super().__init__(code)
        self.client_message = client_message
        self.code = code
        self.category = category
        self.retryable = retryable
        self.retry_reason = retry_reason
        self.retry_after_s = retry_after_s
        # True when the run stopped before any engine call: a cancel while the core waits to retry then cancels cleanly.
        self.before_send = before_send


@dataclass(frozen=True)
class MultipartHost:
    """What the runner reads from the gateway. Tests inject fakes for each of them."""

    # Service id -> normalized service record (the live registry).
    services: Callable[[], Mapping[str, Mapping[str, Any]]]
    leases: ExclusivePoolLeaseManager
    # A fresh client per run, closed when the run ends.
    client_factory: Callable[[], httpx.AsyncClient]
    # Raises when the gateway holds no serving lease for the service; called just before the send, never for health.
    require_egress: Callable[[str], None]
    # store_output(caller, WorkflowOutput) -> the Library asset id the finished file was kept under.
    store_output: Callable[[Any, asset_types.WorkflowOutput], str | None]
    # read_asset(asset_id, caller) -> the bytes of a Library asset the caller may read, or None.
    read_asset: Callable[[str, Any], bytes | None]
    jobs_path: Callable[[], Path]
    limits: Callable[[], result_bundle.ResultLimits]
    # Asked once per lease when the pool is free but its memory cannot hold the claim: release what idle engines keep
    # resident. It gets the pool's spec, so it can reach the engines on the pool's devices. Returns True when something
    # was released, ``ENGINE_BUSY`` when an engine is serving. Never called while another run holds the lease.
    release_idle: Callable[[ExclusivePoolSpec], Awaitable[bool | str]] | None = None
    # slot_released(job_id, stop): a block in which the job does not hold its run slot (it waits for a held pool); ``stop``
    # ends the wait for the slot after the block (a cancel). None: no slots.
    slot_released: Callable[[str, Callable[[], Awaitable[None]]], AbstractAsyncContextManager[Any]] | None = None
    # Installed only after dispatch coverage permits replacing the legacy authority.
    admission: Callable[[], AdmissionScheduler | None] = lambda: None


def exclusive_pool_problem(services: Mapping[str, Mapping[str, Any]], service_id: str,
                           capability: str | None = None) -> tuple[str | None, ExclusivePoolSpec | None, Mapping[str, Any] | None]:
    """The selected service's policy: (problem, exclusive pool spec, service). ``capability`` is checked when given."""
    service = services.get(service_id)
    if not isinstance(service, Mapping):
        return SERVICE_ABSENT, None, None
    if service.get("enabled") is False:
        return SERVICE_DISABLED, None, service
    if capability is not None and capability not in (service.get("capabilities") or ()):
        return "service lacks the capability", None, service
    config = config_of(service)
    try:
        spec = parse_resource_capacity(service_id, config.get("resource_capacity"))
    except ValueError as error:
        return str(error), None, service
    if spec is None:
        return "exclusive capacity is not enabled", None, service
    return None, spec, service


def _send_timeout_s(config: Mapping[str, Any]) -> float:
    """Validate the configured send deadline; only absence or null uses the default."""
    value = config.get("timeout")
    if value is None:
        return _DEFAULT_TIMEOUT_S
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunError(SERVICE_UNAVAILABLE, "capacity")
    try:
        timeout = float(value)
    except OverflowError as error:
        raise RunError(SERVICE_UNAVAILABLE, "capacity") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise RunError(SERVICE_UNAVAILABLE, "capacity")
    return timeout


def config_of(service: Mapping[str, Any] | None) -> Mapping[str, Any]:
    value = (service or {}).get("config")
    return value if isinstance(value, Mapping) else {}


def observer_problem(services: Mapping[str, Mapping[str, Any]], spec: ExclusivePoolSpec) -> tuple[str | None, str]:
    """Whether the observer service may be asked at all, and the URL to ask. Reads no capacity."""
    record = services.get(spec.observer_service)
    if not isinstance(record, Mapping) or record.get("enabled") is False:
        return "observer service is disabled or absent", ""
    url = service_base_url(config_of(record)) + spec.observer_path
    if not url.startswith(("http://", "https://")):
        return "observer service is not available", ""
    return None, url


class _ProbeBodyTooLarge(Exception):
    pass


async def _probe_get(client: httpx.AsyncClient, url: str, *, timeout: float, read_body: bool) -> tuple[int, bytes]:
    """One GET bounded in total time and, when its body is read, in bytes. The body is streamed, never buffered
    whole; a health answer is judged on its status and its body is dropped unread."""
    async with asyncio.timeout(timeout):
        # Identity encoding, and an encoded reply is refused before its body is read: a decoder would expand a small reply past the cap.
        async with client.stream("GET", url, timeout=timeout, headers={"Accept-Encoding": "identity"},
                                 extensions=engine_egress.probe()) as response:
            body = bytearray()
            if read_body and response.status_code == 200:
                if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                    raise _ProbeBodyTooLarge  # an encoded body is refused undecoded
                async for chunk in response.aiter_bytes():  # identity only: no decoder runs
                    if len(body) + len(chunk) > MAX_PROBE_BODY_BYTES:
                        raise _ProbeBodyTooLarge
                    body.extend(chunk)
            return response.status_code, bytes(body)


async def read_observer(client: httpx.AsyncClient, spec: ExclusivePoolSpec, url: str) -> Any:
    """The observer's JSON answer. Anything but a 200 with JSON is ``PoolObserverError``."""
    try:
        status, body = await _probe_get(client, url, timeout=spec.timeout_seconds, read_body=True)
    except (httpx.HTTPError, TimeoutError, _ProbeBodyTooLarge) as error:
        raise PoolObserverError("observer did not answer") from error
    if status != 200:
        raise PoolObserverError("observer did not answer")
    try:
        return json.loads(body)
    except ValueError as error:
        raise PoolObserverError("observer answer is not JSON") from error


class PoolProbeHost(Protocol):
    """What a pool probe reads: the live services and a client factory (``MultipartHost`` and the probe's own deps)."""

    services: Callable[[], Mapping[str, Mapping[str, Any]]]
    client_factory: Callable[[], httpx.AsyncClient]


async def probe_pool(host: PoolProbeHost, spec: ExclusivePoolSpec, service: Mapping[str, Any]) -> str | None:
    """The reason the pool cannot take work now, or None. One health GET and one observer GET; nothing is leased."""
    config = config_of(service)
    base = service_base_url(config)
    if not base:
        return "service has no base URL"
    problem, url = observer_problem(host.services(), spec)
    if problem is not None:
        return problem
    async with host.client_factory() as client:
        try:
            health_status, _ = await _probe_get(client, base + str(config.get("health_endpoint") or "/health"),
                                                timeout=_HEALTH_TIMEOUT_S, read_body=False)
        except Exception:  # noqa: BLE001 - any failure to ask is one bounded reason, never the raw error
            return "service health did not answer"
        if not 200 <= health_status < 300:
            return "service health did not answer with 2xx"
        try:
            observed_status, observed_body = await _probe_get(client, url, timeout=spec.timeout_seconds, read_body=True)
        except Exception:  # noqa: BLE001
            return "observer did not answer"
        if observed_status != 200:
            return "observer did not answer with 200"
        try:
            document = json.loads(observed_body)
        except ValueError:
            return "observer answer is not JSON"
    try:
        observation_from_document(document, spec)
    except PoolObserverError as error:
        return f"observer facts are not usable: {error}"
    return None


# The protocol description this runner speaks (registry/protocols).
PROTOCOL_ID = protocol_description.shipped_for_implementation("multipart_post").id


def runner(host: MultipartHost, description: protocol_description.ProtocolDescription | None = None) -> "MultipartRunner":
    """The runner for the shipped protocol description, over ``host``."""
    return MultipartRunner(host, description or protocol_description.load_registry()[PROTOCOL_ID])


def runners(host: MultipartHost) -> tuple["MultipartRunner", ...]:
    """One runner per validated description selecting this reviewed implementation."""
    return tuple(runner(host, doc) for doc in protocol_description.load_registry().values()
                 if doc.implementation == "multipart_post")


class MultipartRunner:
    """``workflow_adapters.BindingRunner`` for the multipart protocol description."""

    def __init__(self, host: MultipartHost, description: protocol_description.ProtocolDescription) -> None:
        if description.implementation != "multipart_post" or not description.multipart:
            raise ValueError("The protocol requires reviewed multipart traits")
        self._fields = description.multipart
        self._host = host
        self._description = description
        self.binding_key = description.binding or description.id
        self.adapter_id = description.adapter_identity

    # -- before a job exists -----------------------------------------------------------------------------------

    def binding_problem(self, binding: Any) -> str | None:
        problem, _, _ = exclusive_pool_problem(self._host.services(), str(_body(binding).get(self._fields["service"]) or ""))
        return None if problem is None else BINDING_UNAVAILABLE

    def identity(self, entry: Any, _binding_key: str, binding: Any) -> AdapterIdentity:
        service_id = str(_body(binding).get(self._fields["service"]) or "")
        capability = entry.contract.get("capability")
        problem, _, service = exclusive_pool_problem(
            self._host.services(), service_id, capability if isinstance(capability, str) else None)
        if problem is not None:
            raise ValueError(BINDING_UNAVAILABLE)
        return AdapterIdentity(
            protocol_id=self._description.id, protocol_revision=self._description.revision,
            adapter_id=self.adapter_id, interface_version=ADAPTER_INTERFACE_VERSION_V2, service_id=service_id,
            origin=service_base_url(config_of(service)))

    # -- the run -------------------------------------------------------------------------------------------------

    async def run(self, ctx: RunContext) -> None:
        path = self._host.jobs_path()
        job = media_jobs.get(ctx.job_id, audience="internal", include_deleted=True, path=path) or {}
        if media_jobs.is_terminal(str(job.get("status") or "")):
            return
        if (job.get("metadata") or {}).get("protocol_submitting"):
            # A send whose answer was never committed is not repeated: the service may have done the work.
            raise RunError(SUBMISSION_UNKNOWN, "upstream")
        binding = _body(ctx.pin.binding)
        service_id = str(binding.get(self._fields["service"]) or "")
        capability = ctx.pin.contract.get("capability")
        problem, spec, service = exclusive_pool_problem(
            self._host.services(), service_id, capability if isinstance(capability, str) else None)
        if problem is not None or spec is None or service is None:
            raise RunError(SERVICE_UNAVAILABLE, "capacity", retryable=True)
        config = config_of(service)
        base = service_base_url(config)
        if not base:
            raise RunError(SERVICE_UNAVAILABLE, "capacity", retryable=True)
        file_field, file, fields = await self._form(ctx, binding)
        observer_issue, observer_url = observer_problem(self._host.services(), spec)
        client = self._host.client_factory()
        gpu_wait_s = retry_policy(ctx.pin.binding).gpu_wait_s
        if gpu_wait_s > 0:
            admission_remaining(ctx.job_id, gpu_wait_s, path=path)
        async with AsyncExitStack() as resources:
            resources.push_async_callback(client.aclose)

            async def ready() -> None:
                if not await _healthy(client, base, config):
                    raise RunError(SERVICE_UNAVAILABLE, "capacity", retryable=True)
                try:
                    self._host.require_egress(service_id)
                except Exception as error:  # noqa: BLE001 - a refused send is one bounded outcome
                    LOG.info("multipart egress refused: %s", type(error).__name__)
                    raise RunError(SERVICE_UNAVAILABLE, "capacity", retryable=True) from error

            await ready()
            try:
                if gpu_wait_s > 0:
                    remaining = admission_remaining(ctx.job_id, gpu_wait_s, path=path)
                    if remaining <= 0:
                        raise PoolCapacityError(AdmissionStatus.DELAYED, lease_held=True)
                else:
                    remaining = 0
                await self._lease_and_run(
                    ctx, client, resources, spec, (observer_issue, observer_url), base + str(binding.get(self._fields["path"]) or ""), file_field, file,
                    fields, _send_timeout_s(config), gpu_wait_s=remaining, ready=ready)
            except AdmissionExpired as error:
                self._tell_retry_after(ctx.job_id, spec)
                raise RunError(RESOURCE_UNAVAILABLE, "capacity", retryable=True,
                               client_message=GPU_BUSY_COPY, before_send=True) from error
            except (LeaseWaitStopped, AdmissionWithdrawn) as error:
                # A cancel while the job waited its turn: nothing was sent, so the job ends cancelled.
                raise media_jobs.CancellationPending("Cancel requested while the job waited for the GPU") from error
            except (PoolCapacityError, PoolObserverError) as error:
                if isinstance(error, PoolCapacityError):
                    LOG.info("multipart capacity refused: %s lease_held=%s release=%s",
                             error, error.lease_held, error.release)
                else:
                    LOG.info("multipart capacity refused: %s", error)
                if isinstance(error, PoolCapacityError) and error.status is AdmissionStatus.REJECTED:
                    raise RunError(RESOURCE_UNAVAILABLE, "capacity", retryable=False,
                                   client_message="The resource claim exceeds the graphics card capacity.",
                                   before_send=True) from error
                if isinstance(error, PoolCapacityError) and error.lease_held and gpu_wait_s > 0:
                    # The job waited its turn for the whole budget. A retry would wait again, so the job fails as busy and
                    # its caller may ask again. The wait wrote the retry delay when it started (``_turn``).
                    raise RunError(RESOURCE_UNAVAILABLE, "capacity", retryable=True, client_message=GPU_BUSY_COPY,
                                   before_send=True) from error
                self._tell_retry_after(ctx.job_id, spec)
                # Every other refusal is waitable. An engine is busy, the release freed memory that is still short, the
                # observer did not answer, or (with no wait budget) a run holds the lease: the retry is a new lease, which
                # asks the release hook again.
                raise RunError(
                    RESOURCE_UNAVAILABLE, "capacity", retryable=True, client_message=GPU_BUSY_COPY,
                    retry_reason="gpu_busy", retry_after_s=float(spec.retry_after_seconds), before_send=True,
                ) from error
            except MultipartFailure as error:
                LOG.warning("multipart downstream failed: %s", error)
                raise _run_error(error) from error

    async def _lease_and_run(self, ctx: RunContext, client: httpx.AsyncClient, resources: AsyncExitStack, spec: ExclusivePoolSpec,
                              observer: tuple[str | None, str], url: str, file_field: str, file: ImageAsset,
                              fields: Mapping[str, str], timeout: float, *, gpu_wait_s: float,
                              ready: Callable[[], Awaitable[None]]) -> None:
        """One lease, held from the observation until the service answers. A pool whose lease is held is waited for in
        turn (``_turn``); with no wait budget it is refused at once."""
        observer_issue, observer_url = observer

        async def observe() -> Any:
            if observer_issue is not None:
                raise PoolObserverError(observer_issue)
            return await read_observer(client, spec, observer_url)

        admission = self._host.admission()
        lease = None
        if admission is not None:
            physical = physical_resource_ids(spec)
            if not physical:
                raise RunError(SERVICE_UNAVAILABLE, "capacity", client_message="Physical resource mapping is missing.")
            job = media_jobs.get(ctx.job_id, audience="internal", path=self._host.jobs_path()) or {}
            deadline = (job.get("metadata") or {}).get("gpu_admission_deadline", time.time() + gpu_wait_s)
            async def capacity_ready() -> bool:
                try:
                    observed = observation_from_document(await observe(), spec)
                except PoolObserverError:
                    return False
                snapshot, claim = snapshot_and_claim(spec, observed, observed_at=time.time())
                if claim.vram_mb + math.ceil(spec.reserve_bytes / MIB) > snapshot.total_vram_mb:
                    raise PoolCapacityError(AdmissionStatus.REJECTED)
                return evaluate_admission(AdmissionRequest(ctx.job_id, claim), snapshot).accepted
            async def stop() -> None:
                with job_wake.watch(ctx.job_id) as wake:
                    while not self._cancel_asked(ctx.job_id):
                        await wake.event.wait()
                        wake.event.clear()
            def waiting() -> None:
                self._tell_retry_after(ctx.job_id, spec)
                self._tell_gpu_wait(ctx.job_id, max(0, deadline - time.time()))
            lease = await resources.enter_async_context(admission.admit(
                request_id=ctx.job_id, caller=str(job.get("owner_scope") or "unscoped"),
                policy_revision=str(ctx.pin.envelope.record_revision),
                service_id=str(_body(ctx.pin.binding).get(self._fields["service"]) or ""),
                resources=physical, claim_envelope={"max_claim_bytes": spec.max_claim_bytes,
                                                    "reserve_bytes": spec.reserve_bytes},
                deadline=deadline, ready=capacity_ready, stop=stop,
                no_wait=retry_policy(ctx.pin.binding).gpu_wait_s == 0,
                on_wait=waiting, on_ready=lambda waited: self._tell_gpu_ready(ctx.job_id, waited)))
            await ready()
        else:
            waited: list[float] = []
            await resources.enter_async_context(
                self._host.leases.lease(spec, request_id=ctx.job_id, observe=observe,
                                          wait=self._turn(ctx.job_id, spec, gpu_wait_s, waited) if gpu_wait_s > 0 else False,
                                          release_idle=(functools.partial(self._host.release_idle, spec)
                                                        if self._host.release_idle is not None else None)))
            if waited:
                if self._cancel_asked(ctx.job_id):
                    raise media_jobs.CancellationPending("Cancel requested while the job waited for the GPU")
                await ready()
        prepared = _MultipartRun(self, ctx, client, url, file_field, file, fields, timeout, resources.aclose, lease)
        await declared_operation_job.run(prepared, str(ctx.pin.contract["intent"]),
                                         {**ctx.generated, **ctx.inputs}, job_id=ctx.job_id, caller=ctx.caller)

    def _turn(self, job_id: str, spec: ExclusivePoolSpec, budget_s: float, waited: list[float]) -> LeaseWait:
        """How this job waits for a pool another job holds: in turn, up to ``budget_s``, without its run slot, until a
        cancel. ``waited`` gets the seconds waited once the job holds the lease.

        The pool's retry delay is written when the wait starts: a sync caller that stops waiting for a job that still waits
        for the GPU is told to ask again after it (``Shape.to_response``), and a wait that runs out fails with it."""
        slot_released = self._host.slot_released

        @asynccontextmanager
        async def queued() -> Any:
            started = time.monotonic()
            self._tell_retry_after(job_id, spec)
            self._tell_gpu_wait(job_id, budget_s)
            async with (slot_released(job_id, cancelled) if slot_released is not None else nullcontext()):
                yield
            waited.append(time.monotonic() - started)
            self._tell_gpu_ready(job_id, waited[-1])

        async def cancelled() -> None:
            # Woken by the cancel itself (``request_cancel`` wakes the job's watchers); no polling. The wake is in-process
            # only: a cancel another process writes would not end this wait until the budget does. One gateway process
            # serves the pool (the lease is process-local too), so the cancel route runs in this process.
            with job_wake.watch(job_id) as woken:
                while not self._cancel_asked(job_id):
                    await woken.event.wait()
                    woken.event.clear()

        return LeaseWait(budget_s=budget_s, queued=queued, stop=cancelled)

    def _cancel_asked(self, job_id: str) -> bool:
        try:
            job = media_jobs.get(job_id, audience="internal", include_deleted=True, path=self._host.jobs_path()) or {}
        except Exception:  # noqa: BLE001 - an unread job is not a cancel; the next wake reads it again
            LOG.exception("the job was not read while it waited for the GPU")
            return False
        return media_jobs.is_terminal(str(job.get("status") or "")) or bool((job.get("metadata") or {}).get("cancel_requested_at"))

    def _progress(self, job_id: str) -> int:
        job = media_jobs.get(job_id, audience="internal", include_deleted=True, path=self._host.jobs_path()) or {}
        return int(job.get("progress") or 0)

    def _tell_gpu_wait(self, job_id: str, budget_s: float) -> None:
        """One event per wait: the job is running but waits its turn for the GPU. Progress does not move."""
        try:
            media_jobs.append_event(job_id, "waiting_for_gpu", {
                "phase": "waiting_for_gpu", "status": media_jobs.STATUS_RUNNING, "progress": self._progress(job_id),
                "reason": "gpu_busy", "wait_s": int(budget_s)}, path=self._host.jobs_path())
        except Exception:  # noqa: BLE001 - the event is a courtesy; the job still waits
            LOG.exception("the GPU wait was not recorded")

    def _tell_gpu_ready(self, job_id: str, waited_s: float) -> None:
        """The legacy wait ended. A sync caller uses this event to stop treating
        the job as waiting. Retry one failed event write; ticket-based admission
        uses persisted phase and atomic withdrawal instead of this courtesy event.
        """
        for attempt in (1, 2):
            try:
                media_jobs.append_event(job_id, "gpu_ready", {
                    "phase": "running", "status": media_jobs.STATUS_RUNNING, "progress": self._progress(job_id),
                    "waited_s": int(waited_s)}, path=self._host.jobs_path())
                return
            except Exception:  # noqa: BLE001 - the event is a courtesy; the job still goes on
                LOG.exception("the end of the GPU wait was not recorded (try %d of 2)%s", attempt,
                              "; a sync caller that times out may read this job as still waiting" if attempt == 2 else "")

    def _tell_retry_after(self, job_id: str, spec: ExclusivePoolSpec) -> None:
        try:
            job_retry_hint.write(job_id, spec.retry_after_seconds, path=self._host.jobs_path())
        except Exception:  # noqa: BLE001 - the delay is a courtesy; the job still fails as retryable
            LOG.exception("the retry delay was not recorded")

    async def _form(self, ctx: RunContext, binding: Mapping[str, Any]) -> tuple[str, ImageAsset, dict[str, str]]:
        """The file part's field name and bytes, and the flat scalar fields, from the job's pinned binding."""
        contract_inputs = ctx.pin.contract.get("inputs") or {}
        file_field: str | None = None
        file: ImageAsset | None = None
        for logical, target in (binding.get(self._fields["map"]) or {}).items():
            if (contract_inputs.get(logical) or {}).get("type") == "asset":
                refs = ctx.media.get(logical) or ()
                if len(refs) != 1:
                    raise RunError(INPUT_UNAVAILABLE, "invalid_input")
                file_field, file = str(target), await self._read_file(ctx, refs[0])
        if file is None or file_field is None:
            raise RunError(INPUT_UNAVAILABLE, "invalid_input")
        return file_field, file, scalar_fields(contract_inputs, binding.get(self._fields["map"]) or {}, {**ctx.generated, **ctx.inputs})

    async def _read_file(self, ctx: RunContext, ref: Any) -> ImageAsset:
        if ref.spool_key is not None:
            source = ctx.spool.path(ctx.job_id, ref.spool_key)
            try:
                data = await asyncio.to_thread(source.read_bytes)
            except OSError as error:
                raise RunError(INPUT_UNAVAILABLE, "internal") from error
        else:
            data = await asyncio.to_thread(self._host.read_asset, str(ref.asset_id), ctx.caller)
            if data is None:
                raise RunError(INPUT_UNAVAILABLE, "invalid_input")
        suffix = asset_types.suffix_for_media_type(ref.media_type)
        return ImageAsset(data, "input" + suffix, ref.media_type)

    async def _result(self, ctx: RunContext, file: ImageAsset, answer: bytes) -> ResultBundle:
        """Keep the answer with its input provenance; core publishes the returned bundle."""
        (name, output), = (ctx.pin.contract.get("outputs") or {}).items()
        media_type = str(output.get("media_type"))
        stored = asset_types.WorkflowOutput(
            data=answer, suffix=asset_types.suffix_for_media_type(media_type),
            workflow_name=ctx.pin.contract.get("display_name"), request_id=ctx.job_id,
            inputs=(asset_types.OutputInput(file.data, _source_name(ctx), file.content_type),), takes_text=False)
        try:
            asset_id = await asyncio.to_thread(self._host.store_output, ctx.caller, stored)
        except result_bundle.ResultRefused:
            raise
        except Exception:  # noqa: BLE001 - a result that cannot be kept fails the job, as an unsaved result must
            LOG.exception("the result was not stored")
            asset_id = None
        if not asset_id:
            raise RunError(MODEL_NOT_KEPT, "internal")
        return ResultBundle({name: AssetsResult((AssetRef(asset_id, media_type, len(answer)),))})


@dataclass
class _MultipartRun:
    """One prepared multipart invocation; the generic runner owns its durable completion."""

    runner: MultipartRunner
    ctx: RunContext
    client: httpx.AsyncClient
    url: str
    file_field: str
    file: ImageAsset
    fields: Mapping[str, str]
    timeout: float
    finish_send: Callable[[], Awaitable[None]]
    admission: AdmissionLease | None = None
    interface_version = ADAPTER_INTERFACE_VERSION_V2

    @property
    def adapter(self) -> _MultipartRun:
        return self

    def connection_for(self, op: str) -> None:
        return None  # The checked, leased client above is this invocation's connection.

    def request(self, op: str, body: Mapping[str, Any]) -> CanonicalRequest:
        return CanonicalRequest(op=op, body=body)

    def poll_timing(self, op: str) -> tuple[float, float]:
        return 0, self.timeout  # This adapter always completes in start.

    def result_context(self) -> ResultContext:
        return ResultContext(self.runner._host.limits())

    async def start(self, conn: Any, op: str, request: CanonicalRequest) -> Completed:
        if self.admission is not None:
            if self.runner._cancel_asked(self.ctx.job_id):
                raise AdmissionWithdrawn()
            if not await self.admission.commit_send():
                phase = (await asyncio.to_thread(self.admission.store.ticket, self.ctx.job_id)).phase
                if phase == "expired":
                    raise AdmissionExpired()
                raise AdmissionWithdrawn()
        try:
            async with asyncio.timeout(self.timeout):
                answer = await post_multipart(
                    self.client, self.url, image_field=self.file_field, image=self.file,
                    fields=self.fields, timeout_seconds=self.timeout, max_response_bytes=MAX_ANSWER_BYTES)
        except TimeoutError as error:
            raise MultipartFailure(KIND_TIMEOUT) from error
        except MultipartFailure as error:
            if self.admission is not None and error.kind == KIND_UPSTREAM and error.status is not None and 400 <= error.status < 500:
                self.admission.completed("upstream rejected synchronous request")
            raise
        if self.admission is not None:
            self.admission.completed("synchronous operation returned its complete response")
        await self.finish_send()  # Release the pool and close the client before Library storage, as before.
        return Completed(await self.runner._result(self.ctx, self.file, answer))


def _body(binding: Any) -> Mapping[str, Any]:
    """The binding block, whether given as a ``BindingDef`` or as the pinned mapping."""
    body = getattr(binding, "binding", binding)
    return body if isinstance(body, Mapping) else {}


async def _healthy(client: httpx.AsyncClient, base: str, config: Mapping[str, Any]) -> bool:
    try:
        status, _ = await _probe_get(client, base + str(config.get("health_endpoint") or "/health"),
                                     timeout=_HEALTH_TIMEOUT_S, read_body=False)
    except (httpx.HTTPError, TimeoutError):
        return False
    return 200 <= status < 300


def _run_error(error: MultipartFailure) -> RunError:
    if error.kind == KIND_TIMEOUT:
        return RunError("upstream_timeout", "timeout")
    if error.kind == KIND_CONNECT:
        return RunError("upstream_unavailable", "upstream")
    if error.kind in (KIND_UPSTREAM, KIND_MALFORMED):
        return RunError("upstream_error", "upstream")
    return RunError("upstream_error", "upstream")


def _source_name(ctx: RunContext) -> str:
    refs = next(iter(ctx.media.values()), ())
    return str((refs[0].filename if refs else None) or "input")


def scalar_fields(contract_inputs: Mapping[str, Any], mapping: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, str]:
    """The flat form fields a binding sends: each mapped, non-asset input from ``values``, else its contract default."""
    fields: dict[str, str] = {}
    for logical, target in mapping.items():
        spec = contract_inputs.get(logical) or {}
        if spec.get("type") == "asset":
            continue
        if logical in values:
            fields[str(target)] = _scalar(values[logical])
        elif "default" in spec:
            fields[str(target)] = _scalar(spec["default"])
    return fields


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RunError(INPUT_UNAVAILABLE, "invalid_input")
        return repr(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))

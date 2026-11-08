"""The reviewed runner for a binding that sends one multipart form to a service and reads the finished bytes back.

The run core dispatches here (``workflow_adapters``) for the binding key the protocol description names. Everything the
run needs comes from the job's pin (the contract, the binding) and from ``MultipartHost`` (the gateway's services, its
exclusive-pool leases, its Library). Nothing here names a workflow, an intent or a service.

One run:

1. The service must be present, enabled, and declare an enabled exclusive pool (``binding_problem``, also asked before a
   job exists).
2. The service's health is read. The egress guard is asked just before the send.
3. The pool's lease is taken without waiting. A busy pool, or an observer that does not answer, fails the job with
   ``resource_unavailable`` (retryable). The pool's retry delay is written to the job as a ``retry_after`` event.
4. The lease is held from the observation until the service answers or the wall-clock bound ends. The service has no
   cancel call: a cancel of a running job waits for the answer, and the answer is then dropped.
5. The model is kept in the Library and published as the job's one result.

The attempt marker (``protocol_submitting``) is written just before the send and cleared in the commit that publishes the
result, as for every adapter. A run that meets the marker without a result does not send again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import httpx

from gateway import asset_types, engine_egress, job_retry_hint, media_jobs, result_bundle
from gateway.engine_protocol import description as protocol_description
from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION_V2
from gateway.exclusive_pool import (
    MIB,
    ExclusivePoolLeaseManager,
    ExclusivePoolSpec,
    PoolCapacityError,
    PoolObserverError,
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
from gateway.mesh_manifest import ImageAsset
from gateway.service_registry import service_base_url
from gateway.workflow_run import BINDING_UNAVAILABLE, AdapterIdentity, RunContext

LOG = logging.getLogger("azizam.multipart")

MAX_FILE_BYTES = 25 * MIB
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
    """A run that failed. ``code`` and ``category`` are what the job records; the text never reaches the job."""

    def __init__(self, code: str, category: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.category = category
        self.retryable = retryable


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
PROTOCOL_ID = "http-multipart"


def runner(host: MultipartHost) -> "MultipartRunner":
    """The runner for the shipped protocol description, over ``host``."""
    return MultipartRunner(host, protocol_description.load_registry()[PROTOCOL_ID])


class MultipartRunner:
    """``workflow_adapters.BindingRunner`` for the multipart protocol description."""

    def __init__(self, host: MultipartHost, description: protocol_description.ProtocolDescription) -> None:
        self._host = host
        self._description = description
        self.binding_key = description.binding or description.id
        self.adapter_id = description.adapter_identity

    # -- before a job exists -----------------------------------------------------------------------------------

    def binding_problem(self, binding: Any) -> str | None:
        problem, _, _ = exclusive_pool_problem(self._host.services(), str(_body(binding).get("service") or ""))
        return None if problem is None else BINDING_UNAVAILABLE

    def identity(self, entry: Any, _binding_key: str, binding: Any) -> AdapterIdentity:
        service_id = str(_body(binding).get("service") or "")
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
        job = media_jobs.get(ctx.job_id, audience="internal", path=path) or {}
        if media_jobs.is_terminal(str(job.get("status") or "")):
            return
        if (job.get("metadata") or {}).get("protocol_submitting"):
            # A send whose answer was never committed is not repeated: the service may have done the work.
            raise RunError(SUBMISSION_UNKNOWN, "upstream")
        binding = _body(ctx.pin.binding)
        service_id = str(binding.get("service") or "")
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
        try:
            if not await _healthy(client, base, config):
                raise RunError(SERVICE_UNAVAILABLE, "capacity", retryable=True)
            try:
                self._host.require_egress(service_id)
            except Exception as error:  # noqa: BLE001 - a refused send is one bounded outcome
                LOG.info("multipart egress refused: %s", type(error).__name__)
                raise RunError(SERVICE_UNAVAILABLE, "capacity", retryable=True) from error
            try:
                answer = await self._lease_and_send(
                    ctx, client, spec, (observer_issue, observer_url), base + str(binding.get("path") or ""), file_field, file,
                    fields, float(config.get("timeout") or _DEFAULT_TIMEOUT_S))
            except (PoolCapacityError, PoolObserverError) as error:
                LOG.info("multipart capacity refused: %s", error)
                self._tell_retry_after(ctx.job_id, spec)
                raise RunError(RESOURCE_UNAVAILABLE, "capacity", retryable=True) from error
            except MultipartFailure as error:
                LOG.warning("multipart downstream failed: %s", error)
                raise _run_error(error) from error
        finally:
            await client.aclose()
        await self._publish(ctx, file, answer)

    async def _lease_and_send(self, ctx: RunContext, client: httpx.AsyncClient, spec: ExclusivePoolSpec,
                              observer: tuple[str | None, str], url: str, file_field: str, file: ImageAsset,
                              fields: Mapping[str, str], timeout: float) -> bytes:
        """One lease, held from the observation until the service answers. A pool whose lease is held is refused at
        once, never queued."""
        observer_issue, observer_url = observer

        async def observe() -> Any:
            if observer_issue is not None:
                raise PoolObserverError(observer_issue)
            return await read_observer(client, spec, observer_url)

        async with self._host.leases.lease(spec, request_id=ctx.job_id, observe=observe, wait=False):
            media_jobs.update_status(ctx.job_id, media_jobs.STATUS_RUNNING, metadata_patch={"protocol_submitting": True},
                                     path=self._host.jobs_path())
            try:
                # The client's timeout bounds each read, so a service that sends a byte now and then would hold the
                # lease without end; this bounds the whole call.
                async with asyncio.timeout(timeout):
                    return await post_multipart(client, url, image_field=file_field, image=file, fields=fields,
                                                timeout_seconds=timeout, max_response_bytes=MAX_ANSWER_BYTES)
            except TimeoutError as error:
                raise MultipartFailure(KIND_TIMEOUT) from error

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
        for logical, target in (binding.get("map") or {}).items():
            if (contract_inputs.get(logical) or {}).get("type") == "asset":
                refs = ctx.media.get(logical) or ()
                if len(refs) != 1:
                    raise RunError(INPUT_UNAVAILABLE, "invalid_input")
                file_field, file = str(target), await self._read_file(ctx, refs[0])
        if file is None or file_field is None:
            raise RunError(INPUT_UNAVAILABLE, "invalid_input")
        return file_field, file, scalar_fields(contract_inputs, binding.get("map") or {}, {**ctx.generated, **ctx.inputs})

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

    async def _publish(self, ctx: RunContext, file: ImageAsset, answer: bytes) -> None:
        """Keep the answer in the Library and publish it as the job's one result, clearing the attempt marker."""
        (name, output), = (ctx.pin.contract.get("outputs") or {}).items()
        media_type = str(output.get("media_type"))
        stored = asset_types.WorkflowOutput(
            data=answer, suffix=asset_types.suffix_for_media_type(media_type),
            workflow_name=ctx.pin.contract.get("display_name"), request_id=ctx.job_id,
            inputs=(asset_types.OutputInput(file.data, _source_name(ctx), file.content_type),), takes_text=False)
        try:
            asset_id = await asyncio.to_thread(self._host.store_output, ctx.caller, stored)
        except Exception:  # noqa: BLE001 - a result that cannot be kept fails the job, as an unsaved result must
            LOG.exception("the result was not stored")
            asset_id = None
        if not asset_id:
            raise RunError(MODEL_NOT_KEPT, "internal")
        limits = self._host.limits()

        def stage(conn: Any, attempt: int) -> None:
            result_bundle.stage_assets_in(
                conn, ctx.job_id, attempt, name,
                [{"asset_ref": asset_id, "media_type": media_type, "size_bytes": len(answer)}], limits=limits)

        result_bundle.complete_job(ctx.job_id, stage, limits=limits, metadata_patch={"protocol_submitting": False},
                                   path=self._host.jobs_path())


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

"""Original multipart runner from fd30cbfca, executed with adapter dependencies."""
from __future__ import annotations

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
                    ctx, client, spec, (observer_issue, observer_url), base + str(binding.get(self._fields["path"]) or ""), file_field, file,
                    fields, _send_timeout_s(config))
            except (PoolCapacityError, PoolObserverError) as error:
                LOG.info("multipart capacity refused: %s", error)
                self._tell_retry_after(ctx.job_id, spec)
                raise RunError(RESOURCE_UNAVAILABLE, "capacity", retryable=True, client_message=GPU_BUSY_COPY) from error
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

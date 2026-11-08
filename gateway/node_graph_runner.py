"""The reviewed runner of a node-graph binding: one or more rounds on the engine, the result stages, one committed bundle.

The run core dispatches here (``workflow_adapters``) for the binding key the protocol description names. One run:

1. A run that already saved a prompt id or an attempt marker (a restart, a resume) goes on through the generic job loop
   (``engine_protocol.jobs``), which never submits twice. So does a run whose contract has a result that is not a file.
2. Otherwise the runner binds the pinned graph once per round and runs each round through ``graph_round``, at the poll
   cadence the protocol description declares for the run's operation. A host-supplied ``Batching`` may size the rounds from
   the engine's free memory and may free the engine's idle models once after an out-of-memory refusal; without one the run is
   one round.
3. The files of all rounds go through the binding's ``result_processing`` stages, in order. The Library stores the final
   bytes: the bundle commits once, after the stages.
4. The run gives its result metadata (rounds, files returned, warnings, whether models were freed) to the core before it
   commits, so a waiter that sees the terminal job sees the metadata.

Nothing here names an engine, an intent or a workflow. The reason the core may retry a failure for is data of this module:
``retry_reason`` is set on exactly the failures the old image job route retried.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol

from gateway import generated_defaults, media_jobs, pinned_runtime, result_bundle
from gateway.engine_protocol import graph_round
from gateway.engine_protocol import jobs as protocol_jobs
from gateway.engine_protocol.errors import ProtocolError
from gateway.engine_protocol.types import AssetsResult, CanonicalRequest, ResultBundle, ResultContext
from gateway.workflow_run import AdapterIdentity, RunContext

LOG = logging.getLogger("azizam.node_graph_runner")

# The most files one request returns, whatever the counts it asked for.
MAX_FILES_PER_REQUEST = 10


class RunFailed(Exception):
    """A run that failed. The attributes are what the core records: ``code``, ``category``, ``client_message``,
    ``upstream_status`` and ``retryable``. The core retries only when ``retry_reason`` is set."""

    def __init__(self, code: str, category: str, client_message: str, *, upstream_status: int | None = None,
                 retryable: bool = False, retry_reason: str | None = None, retry_after_s: float | None = None,
                 queue_position: int | None = None) -> None:
        super().__init__(code)
        self.code, self.category, self.client_message = code, category, client_message
        self.upstream_status, self.retryable = upstream_status, retryable
        self.retry_reason, self.retry_after_s, self.queue_position = retry_reason, retry_after_s, queue_position


@dataclass
class Plan:
    """What a host-supplied ``Batching`` says about one run."""

    total: int                                  # files wanted
    max_rounds: int
    warnings: list[str] = field(default_factory=list)
    state: Any = None                           # the host's own record of the run


class Batching(Protocol):
    """The host's sizing of rounds from the engine's free memory (it owns every engine-specific read)."""

    async def plan(self, session: Any, ctx: RunContext, inputs: Mapping[str, Any], options: Mapping[str, Any]) -> Plan: ...

    async def target(self, plan: Plan, session: Any, round_no: int, remaining: int) -> int:
        """How many files the next round asks for."""
        ...

    def round_inputs(self, plan: Plan, target: int) -> Mapping[str, Any]:
        """The inputs that make a round ask for ``target`` files."""
        ...

    def probe(self, plan: Plan, session: Any, round_no: int) -> Any:
        """An object with ``start()`` and ``async stop()`` that watches the engine during the round, or None."""
        ...

    async def measured(self, plan: Plan, session: Any, round_no: int, target: int, probe: Any) -> None:
        """Called after a first successful round."""

    async def free_after_refusal(self, plan: Plan, session: Any) -> bool:
        """Free the engine's idle models once. True when it did."""
        ...


@dataclass(frozen=True)
class GraphHost:
    """What the runner reads from the gateway. Tests inject fakes for each of them."""

    engine: Callable[[str], tuple[str, Mapping[str, Any], Any, Any] | None]    # binding key -> (service, record, protocol, adapter)
    identity: Callable[[Any, str, Any], AdapterIdentity]
    problem: Callable[[Any], str | None]
    adapter: Callable[[Mapping[str, Any], Any, RunContext], Any]               # the adapter over the person's asset store
    connection: Callable[[str, Mapping[str, Any]], Any]
    limits: Callable[[], result_bundle.ResultLimits]
    max_wait_s: Callable[[], float]
    control_timeout: Callable[[], Any]
    headers: Callable[[RunContext], Mapping[str, str]]
    batching: Callable[[RunContext, Any], Batching | None]
    stages: Mapping[str, Callable[..., Awaitable[list[bytes]]]]
    say: Callable[[str], None] = lambda text: None
    # Each media input of the run, held by the engine, as the name the engine gives it: ``(ctx, adapter, connection) -> {input: name}``.
    stage_media: Callable[..., Awaitable[Mapping[str, str]]] | None = None


def retry_reason_of(kind: str, binding_key: str) -> str | None:
    """The reason the old image job route retried a failure of this kind, or None for a failure it never retried."""
    return {graph_round.UNREACHABLE: f"{binding_key}_unavailable", graph_round.NO_PROMPT_ID: "missing_prompt_id",
            graph_round.SUBMIT_TIMEOUT: f"{binding_key}_timeout", graph_round.WAIT_EXCEEDED: f"{binding_key}_timeout"}.get(kind)


def failure_of(failed: graph_round.RoundFailure, adapter: Any, conn: Any, binding_key: str) -> RunFailed:
    """The job failure a round that produced no files becomes (its HTTP status is the one the old route answered with)."""
    from gateway import client_errors
    kind = failed.kind
    reason = retry_reason_of(kind, binding_key)
    engine = adapter.label
    if kind == graph_round.SUBMIT_TIMEOUT:
        return RunFailed("upstream_timeout", "timeout", f"{engine} request timeout", upstream_status=504, retryable=True, retry_reason=reason)
    if kind == graph_round.UNREACHABLE:
        return RunFailed("upstream_unavailable", "upstream", adapter.unavailable_message(conn), upstream_status=502, retryable=True,
                         retry_reason=reason)
    if kind == graph_round.DROPPED:
        return RunFailed("upstream_dropped", "upstream", "Image backend connection dropped", upstream_status=502)
    if kind == graph_round.REFUSED:
        out = graph_round.says_out_of_memory(failed.excerpt, plain=True)
        text = client_errors.REASONS["upstream_out_of_memory" if out else "upstream_refused"]
        return RunFailed("upstream_out_of_memory" if out else "upstream_refused", "upstream", f"{engine}: {text}",
                         upstream_status=502)
    if kind == graph_round.NO_PROMPT_ID:
        return RunFailed("upstream_no_prompt_id", "upstream", "No prompt_id returned", upstream_status=502, retryable=True, retry_reason=reason)
    if kind == graph_round.EXECUTION_FAILED:
        return RunFailed("upstream_job_failed", "upstream", failed.detail or "The upstream job did not succeed.", upstream_status=502)
    if kind == graph_round.WAIT_EXCEEDED:
        suffix = f" (queue_position={failed.queue_position})" if failed.queue_position is not None else ""
        return RunFailed("image_generation_timeout", "timeout", f"Image generation timed out before {engine} returned an image{suffix}",
                         upstream_status=504, retryable=True, retry_reason=reason, queue_position=failed.queue_position)
    code = failed.code or "result_refused"
    return RunFailed(code, "upstream", "The result could not be read within the deployment's limits",
                     upstream_status=504 if code == "fetch_timeout" else 502)


def _only_files(contract: Mapping[str, Any]) -> bool:
    """Whether every output the contract declares is a file (a result that is a value goes through the generic loop)."""
    outputs = contract.get("outputs") or {}
    return bool(outputs) and all(isinstance(spec, Mapping) and str(spec.get("media_type") or "") not in ("", "application/json")
                                 for spec in outputs.values())


class NodeGraphRunner:
    """The runner for every binding of one node-graph protocol (``binding_key`` is the key the description names)."""

    def __init__(self, binding_key: str, adapter_id: str, host: GraphHost) -> None:
        self.binding_key, self.adapter_id, self.host = binding_key, adapter_id, host

    # -- what the core asks before a job exists ---------------------------------------------------------------------

    def binding_problem(self, binding: Any) -> str | None:
        return self.host.problem(binding)

    def identity(self, entry: Any, binding_key: str, binding: Any) -> AdapterIdentity:
        return self.host.identity(entry, binding_key, binding)

    # -- the run ----------------------------------------------------------------------------------------------------

    async def run(self, ctx: RunContext) -> None:
        engine = self.host.engine(ctx.pin.envelope.binding_key)
        if engine is None:
            raise ProtocolError("No reviewed adapter is registered for this job.", code="adapter_not_registered")
        service_id, record, protocol, _ = engine
        adapter = self.host.adapter(record, protocol, ctx)
        runtime = pinned_runtime.PinnedRuntime(
            job_id=ctx.job_id, adapter=adapter, protocol=protocol, connection=lambda: self.host.connection(service_id, record),
            timing=(2.0, float(self.host.max_wait_s())), jobs_path=media_jobs.DB_PATH,
            context=ResultContext(self.host.limits()))
        op = str(ctx.pin.contract["intent"])
        body = {**dict(ctx.inputs), **dict(ctx.generated)}
        staged: dict[str, str] = {}
        if ctx.media and self.host.stage_media is not None:
            staged = dict(await self.host.stage_media(ctx, adapter, lambda: self.host.connection(service_id, record)))
            body.update(staged)
        current = media_jobs.get(ctx.job_id, audience="internal", path=media_jobs.DB_PATH) or {}
        metadata = current.get("metadata") or {}
        if metadata.get("protocol_job") or metadata.get("protocol_submitting") or not _only_files(ctx.pin.contract):
            await protocol_jobs.run(runtime, op, body, job_id=ctx.job_id)
            return
        await self._rounds(ctx, adapter, runtime, op, staged)

    async def _rounds(self, ctx: RunContext, adapter: Any, runtime: Any, op: str, staged: Mapping[str, str]) -> None:
        host = self.host
        poll = adapter.poll_for(op)
        manifest_inputs = (ctx.pin.contract.get("inputs") or {})
        options = ((ctx.payload.get("body") or {}).get("options") or {})
        template_request = runtime.request(op, {**dict(ctx.inputs), **dict(ctx.generated), **staged})
        conn = runtime.connection_for(op)
        batching = host.batching(ctx, adapter)
        binding = ctx.pin.binding
        outputs = ctx.pin.contract.get("outputs") or {result_bundle.DEFAULT_NAME: {}}
        headers = dict(host.headers(ctx))
        limits = host.limits()
        failure_binding = ctx.pin.envelope.binding_key
        collected: list[bytes] = []
        result_name: str | None = None
        rounds = 0
        models_freed = False
        async with adapter.open(conn) as session:
            plan = await batching.plan(session, ctx, ctx.inputs, options) if batching is not None else Plan(total=1, max_rounds=1)
            offset = 0
            remaining = plan.total
            while remaining > 0 and rounds < plan.max_rounds:
                rounds += 1
                target = await batching.target(plan, session, rounds, remaining) if batching is not None else 1
                probe = batching.probe(plan, session, rounds) if batching is not None else None
                files = await self._one_round(
                    ctx, adapter, session, runtime, op, template_request, manifest_inputs, offset, batching, plan, target,
                    probe, headers, binding, outputs, limits, poll, f"round {rounds}/{plan.max_rounds} (target={target})",
                    failure_binding, conn, retry_freed=lambda: models_freed, staged=staged)
                if files is None:       # one retry after the engine's models were freed
                    models_freed = True
                    files = await self._one_round(
                        ctx, adapter, session, runtime, op, template_request, manifest_inputs, offset, batching, plan, 1,
                        None, headers, binding, outputs, limits, poll, f"round {rounds}/{plan.max_rounds} after model unload",
                        failure_binding, conn, retry_freed=lambda: True, staged=staged)
                names, round_files = files
                result_name = names or result_name
                offset += max(1, len(round_files))
                if not round_files:
                    plan.warnings.append(f"round {rounds} produced 0 files")
                    if rounds >= 2 and not collected:
                        raise RunFailed("upstream_empty", "upstream", f"{adapter.label} returned no images", upstream_status=502)
                    continue
                collected.extend(round_files)
                remaining -= len(round_files)
                if rounds == 1 and batching is not None:
                    await batching.measured(plan, session, rounds, target, probe)
        collected = collected[:plan.total]
        if not collected:
            raise RunFailed("no_result", "upstream", "No images generated", upstream_status=500)
        if len(collected) < plan.total:
            plan.warnings.append(f"requested {plan.total} but produced {len(collected)} after {rounds} round(s)")
        processed = await self._stages(ctx, collected)
        bundle = await self._store(ctx, adapter, processed, result_name or next(iter(outputs)), limits)
        ctx.record_result_metadata({"rounds": rounds, "returned": len(processed), "warnings": list(plan.warnings),
                                    "models_freed": models_freed or bool(getattr(plan.state, "models_freed", False))})
        protocol_jobs._commit_bundle(runtime, ctx.job_id, bundle, clear_marker=True)

    async def _one_round(self, ctx, adapter, session, runtime, op, template_request, manifest_inputs, offset, batching, plan, target,
                         probe, headers, binding, outputs, limits, poll, label, failure_binding, conn, *, retry_freed, staged):
        host = self.host
        values = generated_defaults.offset_values(manifest_inputs, dict(ctx.generated), offset)
        round_body = {**dict(ctx.inputs), **staged, **values, **(dict(batching.round_inputs(plan, target)) if batching is not None else {})}
        request = CanonicalRequest(op=op, body=copy.deepcopy(round_body), params=template_request.params)
        graph, payload = adapter.compile(op, request)
        media_jobs.update_status(ctx.job_id, media_jobs.STATUS_RUNNING, metadata_patch={"protocol_submitting": True},
                                 path=media_jobs.DB_PATH)

        async def cancelled() -> bool:
            job = media_jobs.get(ctx.job_id, audience="internal", path=media_jobs.DB_PATH) or {}
            return bool((job.get("metadata") or {}).get("cancel_requested_at"))

        if probe is not None:
            probe.start()
        try:
            result = await graph_round.run_round(
                adapter, session, payload, headers, graph=graph, label=label, max_wait_s=float(host.max_wait_s()),
                control_timeout=host.control_timeout(), cancel_connection=lambda: runtime.connection_for(op),
                is_disconnected=cancelled, binding=binding, contract_outputs=outputs, limits=limits, cadence=poll, say=host.say)
        except graph_round.RoundFailure as failed:
            if failed.kind == graph_round.DISCONNECTED:
                raise media_jobs.CancellationPending() from failed
            if (batching is not None and failed.out_of_memory and not retry_freed()
                    and await batching.free_after_refusal(plan, session)):
                return None
            raise failure_of(failed, adapter, conn, failure_binding) from failed
        finally:
            if probe is not None:
                try:
                    await probe.stop()
                except Exception:  # noqa: BLE001 - a probe that fails never fails the run
                    LOG.warning("the capacity probe did not stop cleanly", exc_info=True)
        media_jobs.update_status(ctx.job_id, media_jobs.STATUS_RUNNING, metadata_patch={"protocol_job": result.prompt_id,
                                 "protocol_submitting": False}, path=media_jobs.DB_PATH)
        files = [raw for group in result.files.values() for raw in group]
        name = next(iter(result.files)) if len(result.files) == 1 else None
        return name, files

    async def _stages(self, ctx: RunContext, files: list[bytes]) -> list[bytes]:
        for stage in ctx.pin.binding.get("result_processing") or ():
            run = self.host.stages.get(stage)
            if run is None:
                raise RunFailed("stage_unavailable", "internal", "A result stage of this workflow is not available here.")
            files = await run(ctx, files)
        return files

    async def _store(self, ctx: RunContext, adapter: Any, files: list[bytes], name: str, limits: Any) -> ResultBundle:
        from gateway.engine_protocol.assets import served_media_type
        store = result_bundle.LimitedAssetStore(adapter._assets, limits)
        saved = []
        for raw in files:
            content_type = served_media_type(None, raw[:16]) or "application/octet-stream"

            async def chunks(data: bytes = raw) -> AsyncIterator[bytes]:
                yield data

            saved.append(await store.save(chunks(), content_type=content_type, max_bytes=limits.max_asset_bytes, name=None))
        return ResultBundle({name: AssetsResult(tuple(saved))})

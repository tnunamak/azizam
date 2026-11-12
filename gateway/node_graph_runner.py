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

import asyncio
import copy
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol

from gateway import asset_types, generated_defaults, media_delivery, media_jobs, media_metadata, pinned_runtime, result_bundle
from gateway.engine_protocol import graph_round
from gateway.engine_protocol import jobs as protocol_jobs
from gateway.engine_protocol.errors import ProtocolError
from gateway.engine_protocol.types import AssetsResult, CanonicalRequest, ResultBundle, ResultContext
from gateway.workflow_run import REQUEST_FACTS_KEY, AdapterIdentity, RunContext, with_wire_id
from gateway.workflow_runtime_types import AUDIO_FACT, RESULT_REFUSED_MESSAGE

LOG = logging.getLogger("azizam.node_graph_runner")

# The result stage that cuts a picture out when the engine's own cutout did not (``Variant.cutout_required``).
CUTOUT_STAGE = "background_fallback"


def _has_alpha(raw: bytes) -> bool:
    """True when the picture can carry transparency: it has an alpha band or a transparent colour."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as picture:
            return "A" in picture.getbands() or "transparency" in picture.info
    except Exception:  # noqa: BLE001 - a file that is no picture carries no transparency
        return False


# The most files one request returns, whatever the counts it asked for.
MAX_FILES_PER_REQUEST = 10


KEEP_PROMPT_FLAG = "keep_engine_prompt"   # a request's job metadata: keep the engine's prompt id under ENGINE_PROMPT_KEY
ENGINE_PROGRESS_FLAG = "engine_progress"     # a request's job metadata: the job's progress follows what the engine reports
ENGINE_PROMPT_KEY = "engine_prompt"      # the engine's id of the prompt a run submitted, kept as soon as it is known


def output_kind(outputs: Mapping[str, Any]) -> str:
    """What a workflow declares it produces, from its first declared output's media type (``video/mp4`` -> ``video``); empty when it
    declares none. Data from the record, not a branch on a name."""
    for spec in outputs.values():
        kind = str((spec or {}).get("media_type") or "").split("/", 1)[0] if isinstance(spec, Mapping) else ""
        if kind and kind != "application":
            return kind
    return ""


def output_noun(outputs: Mapping[str, Any]) -> str:
    """The plural of ``output_kind`` for a no-result message; a workflow that declares none produces images, as before."""
    kind = output_kind(outputs)
    word = asset_types.kind_names(kind or "image")["plural"]
    return word[:1].lower() + word[1:]


class RunFailed(Exception):
    """A run that failed. The attributes are what the core records: ``code``, ``category``, ``client_message``,
    ``upstream_status`` and ``retryable``. The core retries only when ``retry_reason`` is set."""

    def __init__(self, code: str, category: str, client_message: str, *, upstream_status: int | None = None,
                 retryable: bool | None = None, retry_reason: str | None = None, retry_after_s: float | None = None,
                 queue_position: int | None = None) -> None:
        super().__init__(code)
        self.code, self.category, self.client_message = code, category, client_message
        self.upstream_status = upstream_status
        # A caller may ask again after a timeout, a capacity refusal or an engine that answered with a server error.
        self.retryable = retryable if retryable is not None else (
            category in ("timeout", "capacity") or (category == "upstream" and (upstream_status or 0) >= 500))
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
class Variant:
    """The host's choice of graph for a run. ``edit`` changes a compiled graph into the chosen one (None: the graph as
    compiled); ``facts`` are what the run reports of the choice. When the chosen graph fails, the run is made again on the
    graph as compiled, and reports ``fallback_facts`` instead."""

    edit: Callable[[dict[str, Any]], None] | None
    facts: Mapping[str, Any]
    fallback_facts: Mapping[str, Any] = field(default_factory=dict)
    # The edit the second graph takes: it turns the cutout off explicitly, whatever the compiled graph holds.
    fallback_edit: Callable[[dict[str, Any]], None] | None = None
    # The caller asked for the cutout outright and a graph with it was tried: a result without transparency (the fallback
    # graph's) is cut out by the ``background_fallback`` stage, or the run fails.
    cutout_required: bool = False


@dataclass(frozen=True)
class GraphHost:
    """What the runner reads from the gateway. Tests inject fakes for each of them."""

    engine: Callable[[str, str], tuple[str, Mapping[str, Any], Any, Any] | None]    # (binding key, pinned service id) -> (service, record, protocol, adapter)
    identity: Callable[[Any, str, Any], AdapterIdentity]
    problem: Callable[[Any], str | None]
    adapter: Callable[..., Any]                          # (record, protocol, ctx, sources) -> the adapter over the person's asset store
    connection: Callable[[str, Mapping[str, Any]], Any]    # (service id, record) -> its address and its wait bound
    limits: Callable[[], result_bundle.ResultLimits]
    control_timeout: Callable[[], Any]
    headers: Callable[[RunContext], Mapping[str, str]]
    batching: Callable[[RunContext, Any], Batching | None]
    stages: Mapping[str, Callable[..., Awaitable[list[bytes]]]]
    say: Callable[[str], None] = lambda text: None
    # Each media input of the run, held by the engine, as the name the engine gives it: ``(ctx, adapter, connection) -> {input: name}``.
    stage_media: Callable[..., Awaitable[Mapping[str, str]]] | None = None
    # The id a caller sees for a job, for the stage that stamps the file with the request it answers (None: the placeholder stays).
    wire_id: Callable[[str], str] | None = None
    # The files the run was given, kept in the Library: ``ctx -> [{"asset_id", "name"}]``. The results name them as their sources.
    sources: Callable[[RunContext], Awaitable[list[dict[str, str]]]] | None = None
    # Which graph the run submits when the workflow has a second one for a second answer to the same request (a cutout of the
    # picture): ``ctx -> Variant | None``. None: the graph as compiled.
    variant: Callable[[RunContext], Awaitable["Variant | None"]] | None = None


def retry_reason_of(kind: str, binding_key: str) -> str | None:
    """The reason the old image job route retried a failure of this kind, or None for a failure it never retried."""
    return {graph_round.UNREACHABLE: f"{binding_key}_unavailable", graph_round.NO_PROMPT_ID: "missing_prompt_id",
            graph_round.SUBMIT_TIMEOUT: f"{binding_key}_timeout", graph_round.WAIT_EXCEEDED: f"{binding_key}_timeout"}.get(kind)


def failure_of(failed: graph_round.RoundFailure, adapter: Any, conn: Any, binding_key: str,
               outputs: Mapping[str, Any] | None = None) -> RunFailed:
    """The job failure a round that produced no files becomes (its HTTP status is the one the old route answered with)."""
    from gateway import client_errors
    kind = failed.kind
    reason = retry_reason_of(kind, binding_key)
    engine = adapter.label
    label = asset_types.kind_names(output_kind(outputs or {}) or "image")["label"]
    noun = label[:1].lower() + label[1:]
    article = "an" if noun[:1] in "aeiou" else "a"
    if kind == graph_round.SUBMIT_TIMEOUT:
        return RunFailed("upstream_timeout", "timeout", f"{engine} request timeout", upstream_status=504, retryable=True, retry_reason=reason)
    if kind == graph_round.UNREACHABLE:
        return RunFailed("upstream_unavailable", "upstream", adapter.unavailable_message(conn), upstream_status=502, retryable=True,
                         retry_reason=reason)
    if kind == graph_round.DROPPED:
        return RunFailed("upstream_dropped", "upstream", f"{label} backend connection dropped", upstream_status=502)
    if kind == graph_round.REFUSED:
        out = graph_round.says_out_of_memory(failed.excerpt, plain=True)
        text = client_errors.REASONS["upstream_out_of_memory" if out else "upstream_refused"]
        return RunFailed("upstream_out_of_memory" if out else "upstream_refused", "upstream", f"{engine}: {text}",
                         upstream_status=502)
    if kind == graph_round.ORPHANED:
        return RunFailed("upstream_orphaned", "upstream", f"orphaned: {engine} does not report this prompt_id", upstream_status=502)
    if kind == graph_round.NO_PROMPT_ID:
        return RunFailed("upstream_no_prompt_id", "upstream", "No prompt_id returned", upstream_status=502, retryable=True, retry_reason=reason)
    if kind == graph_round.EXECUTION_FAILED:
        return RunFailed("upstream_job_failed", "upstream", failed.detail or "The upstream job did not succeed.", upstream_status=502)
    if kind == graph_round.WAIT_EXCEEDED:
        suffix = f" (queue_position={failed.queue_position})" if failed.queue_position is not None else ""
        return RunFailed("image_generation_timeout", "timeout", f"{label} generation timed out before {engine} returned {article} {noun}{suffix}",
                         upstream_status=504, retryable=True, retry_reason=reason, queue_position=failed.queue_position)
    code = failed.code or "result_refused"
    return RunFailed(code, "upstream", RESULT_REFUSED_MESSAGE,
                     upstream_status=504 if code == "fetch_timeout" else 502)


def _media_type_of(raw: bytes) -> str:
    from gateway.engine_protocol.assets import served_media_type
    return served_media_type(None, raw[:16]) or "application/octet-stream"


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
        engine = self.host.engine(ctx.pin.envelope.binding_key, ctx.pin.envelope.service_id)
        if engine is None:
            raise ProtocolError("No reviewed adapter is registered for this job.", code="adapter_not_registered")
        service_id, record, protocol, _ = engine
        sources = await self.host.sources(ctx) if self.host.sources is not None else []
        adapter = self.host.adapter(record, protocol, ctx, sources)
        runtime = pinned_runtime.PinnedRuntime(
            job_id=ctx.job_id, adapter=adapter, protocol=protocol, connection=lambda: self.host.connection(service_id, record),
            timing=(2.0, float(self.host.connection(service_id, record).timeouts.total_s)), jobs_path=media_jobs.DB_PATH,
            context=ResultContext(self.host.limits()))
        op = str(ctx.pin.contract["intent"])
        body = {**dict(ctx.inputs), **dict(ctx.generated)}
        staged: dict[str, str] = {}
        if ctx.media and self.host.stage_media is not None:
            staged = dict(await self.host.stage_media(ctx, adapter, lambda: self.host.connection(service_id, record)))
            body.update(staged)
        current = media_jobs.get(ctx.job_id, audience="internal", path=media_jobs.DB_PATH) or {}
        metadata = current.get("metadata") or {}
        if metadata.get("protocol_submitting") or not _only_files(ctx.pin.contract):
            await protocol_jobs.run(runtime, op, body, job_id=ctx.job_id)
            return
        if media_jobs.is_terminal(str(current.get("status") or "")):
            return
        # A run that saved its prompt id goes on from that prompt, through the same stages and commit as a fresh one.
        await self._rounds(ctx, adapter, runtime, op, staged, resume=str(metadata.get("protocol_job") or "") or None)

    async def _rounds(self, ctx: RunContext, adapter: Any, runtime: Any, op: str, staged: Mapping[str, str],
                      resume: str | None = None) -> None:
        host = self.host
        outputs = ctx.pin.contract.get("outputs") or {result_bundle.DEFAULT_NAME: {}}
        variant = await host.variant(ctx) if host.variant is not None else None
        facts: Mapping[str, Any] = variant.facts if variant is not None else {}
        try:
            made = await self._collect(ctx, adapter, runtime, op, staged, variant.edit if variant is not None else None, resume)
        except RunFailed:
            if variant is None or not variant.fallback_facts or resume is not None:
                raise
            # The second graph failed: the same request is made again once, with the cutout turned off in the graph explicitly.
            facts = variant.fallback_facts
            made = await self._collect(ctx, adapter, runtime, op, staged, variant.fallback_edit, None)
        collected, result_name, rounds, models_freed, plan = made
        # From here the engine holds the files under the prompt id the job saved: a restart collects them again (``resume``) and
        # goes through the same stages and commit below.
        needs_cutout = bool(variant is not None and variant.cutout_required and not all(_has_alpha(raw) for raw in collected))
        processed = await self._stages(ctx, collected, needs_cutout=needs_cutout)
        bundle = await self._store(ctx, adapter, processed, result_name or next(iter(outputs)), host.limits())
        freed = models_freed or bool(getattr(plan.state, "models_freed", False))
        ctx.record_result_metadata({"rounds": rounds, "returned": len(processed), "warnings": list(plan.warnings),
                                    **dict(facts), **({"models_freed": True} if freed else {})})
        # The job lists its files and keeps the run's bookkeeping, as the job routes always did (what a job document shows).
        listed = [{"asset_id": asset.id, "media_type": asset.content_type}
                  for result in bundle.results.values() for asset in getattr(result, "assets", ())]
        current = media_jobs.get(ctx.job_id, audience="internal", path=media_jobs.DB_PATH) or {}
        protocol_jobs._commit_bundle(
            runtime, ctx.job_id, bundle, clear_marker=True, progress=100, add_assets=listed or None,
            metadata={"attempts": int(current.get("attempt_number") or 1), "last_http_status": 200, "completed_at": int(time.time())})

    async def _collect(self, ctx: RunContext, adapter: Any, runtime: Any, op: str, staged: Mapping[str, str],
                       edit: Callable[[dict[str, Any]], None] | None,
                       resume: str | None = None) -> tuple[list[bytes], str | None, int, bool, Plan]:
        """The files of all rounds of one graph, the name the engine gave them, the rounds run, and the plan. With ``resume``
        the one round is the saved prompt, waited for and harvested; nothing is submitted."""
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
            plan = (await batching.plan(session, ctx, ctx.inputs, options) if batching is not None and resume is None
                    else Plan(total=1, max_rounds=1))
            if resume is not None:
                # Harvest one saved prompt, but keep its requested batch rather than one file. The count is the one the host's
                # plan reads: ``options.n`` times the per-prompt ``batch_size`` input.
                count = options.get("n")
                count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 1 else 1
                per_prompt = ctx.inputs.get("batch_size")
                per_prompt = per_prompt if isinstance(per_prompt, int) and not isinstance(per_prompt, bool) and per_prompt >= 1 else 1
                plan.total = min(MAX_FILES_PER_REQUEST, count * per_prompt)
            batching = batching if resume is None else None
            offset = 0
            remaining = plan.total
            while remaining > 0 and rounds < plan.max_rounds:
                rounds += 1
                target = await batching.target(plan, session, rounds, remaining) if batching is not None else 1
                probe = batching.probe(plan, session, rounds) if batching is not None else None
                files = await self._one_round(
                    ctx, adapter, session, runtime, op, template_request, manifest_inputs, offset, batching, plan, target,
                    probe, headers, binding, outputs, limits, poll, f"round {rounds}/{plan.max_rounds} (target={target})",
                    failure_binding, conn, retry_freed=lambda: models_freed, staged=staged, edit=edit, resume=resume)
                if files is None:       # one retry after the engine's models were freed
                    models_freed = True
                    files = await self._one_round(
                        ctx, adapter, session, runtime, op, template_request, manifest_inputs, offset, batching, plan, 1,
                        None, headers, binding, outputs, limits, poll, f"round {rounds}/{plan.max_rounds} after model unload",
                        failure_binding, conn, retry_freed=lambda: True, staged=staged, edit=edit)
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
            raise RunFailed("no_result", "upstream", f"No {output_noun(outputs)} generated", upstream_status=500)
        if len(collected) < plan.total:
            plan.warnings.append(f"requested {plan.total} but produced {len(collected)} after {rounds} round(s)")
        return collected, result_name, rounds, models_freed, plan

    async def _one_round(self, ctx, adapter, session, runtime, op, template_request, manifest_inputs, offset, batching, plan, target,
                         probe, headers, binding, outputs, limits, poll, label, failure_binding, conn, *, retry_freed, staged, edit=None, resume=None):
        host = self.host
        values = generated_defaults.offset_values(manifest_inputs, dict(ctx.generated), offset)
        round_body = {**dict(ctx.inputs), **staged, **values, **(dict(batching.round_inputs(plan, target)) if batching is not None else {})}
        request = CanonicalRequest(op=op, body=copy.deepcopy(round_body), params=template_request.params)
        graph, payload = adapter.compile(op, request, edit=edit)
        if resume is None:
            media_jobs.update_status(ctx.job_id, media_jobs.STATUS_RUNNING, metadata_patch={"protocol_submitting": True},
                                     path=media_jobs.DB_PATH)

        async def cancelled() -> bool:
            job = media_jobs.get(ctx.job_id, audience="internal", path=media_jobs.DB_PATH) or {}
            return bool((job.get("metadata") or {}).get("cancel_requested_at"))

        def job_metadata() -> Mapping[str, Any]:
            return (media_jobs.get(ctx.job_id, audience="internal", path=media_jobs.DB_PATH) or {}).get("metadata") or {}

        def keep_prompt(prompt_id: str) -> None:
            """The engine's id of the prompt, kept at once, for the jobs whose request asked for it (a later delete asks the
            engine to forget the prompt). Other jobs leave no trace of it in their document."""
            if job_metadata().get(KEEP_PROMPT_FLAG):
                media_jobs.update_status(ctx.job_id, media_jobs.STATUS_RUNNING, metadata_patch={ENGINE_PROMPT_KEY: prompt_id},
                                         path=media_jobs.DB_PATH)

        if probe is not None:
            probe.start()
        try:
            result = await graph_round.run_round(
                adapter, session, payload, headers, graph=graph, label=label, max_wait_s=runtime.wait_s,
                control_timeout=host.control_timeout(), resume_prompt_id=resume, cancel_connection=lambda: runtime.connection_for(op),
                is_disconnected=cancelled, binding=binding, contract_outputs=outputs, limits=limits, cadence=poll, say=host.say,
                on_progress=ctx.record_progress if job_metadata().get(ENGINE_PROGRESS_FLAG) else (lambda _percent: None),
                on_submitted=lambda prompt_id: keep_prompt(prompt_id))
        except graph_round.RoundFailure as failed:
            # The submit marker only means "a submit may be in flight": a round that ended in failure leaves none.
            if resume is None:
                media_jobs.update_status(ctx.job_id, media_jobs.STATUS_RUNNING, metadata_patch={"protocol_submitting": False},
                                         path=media_jobs.DB_PATH)
            if failed.kind == graph_round.DISCONNECTED:
                raise media_jobs.CancellationPending() from failed
            if (batching is not None and failed.out_of_memory and not retry_freed()
                    and await batching.free_after_refusal(plan, session)):
                return None
            if failed.queue_position is not None:       # a timed-out caller is told where its job stood in the engine's queue
                ctx.record_result_metadata({"queue_position": failed.queue_position})
            raise failure_of(failed, adapter, conn, failure_binding, outputs) from failed
        finally:
            if probe is not None:
                try:
                    await probe.stop()
                except Exception:  # noqa: BLE001 - a probe that fails never fails the run
                    LOG.warning("the capacity probe did not stop cleanly", exc_info=True)
        media_jobs.update_status(ctx.job_id, media_jobs.STATUS_RUNNING, metadata_patch={"protocol_job": result.prompt_id,
                                 "protocol_submitting": False}, path=media_jobs.DB_PATH)
        files = [raw for group in result.files.values() for raw in group]
        if result.names and result.names[0]:        # the first file's name: what a content route names its download by
            ctx.record_result_metadata({"filename": result.names[0]})
        name = next(iter(result.files)) if len(result.files) == 1 else None
        return name, files

    # -- the reviewed stages that read the run's own facts (a host may still supply a stage of the same name) ----------------

    async def _strip_audio(self, ctx: RunContext, files: list[bytes]) -> list[bytes]:
        """Remove the audio track when the request asked for a silent result (``options.audio`` is false), before the bytes
        are stored, so the stored copy and the delivered file agree."""
        if ((ctx.payload.get("body") or {}).get("options") or {}).get(AUDIO_FACT) is not False:
            return files
        out: list[bytes] = []
        for raw in files:
            try:
                out.append(await asyncio.to_thread(media_metadata.strip_audio, raw, _media_type_of(raw)))
            except media_metadata.MediaMetadataError as exc:
                LOG.warning("audio strip failed: %s", type(exc).__name__)
                raise RunFailed("audio_strip_failed", "upstream", "Could not remove the audio track from the video",
                                upstream_status=502) from exc
        return out

    async def _attach_provenance(self, ctx: RunContext, files: list[bytes]) -> list[bytes]:
        """Stamp each file with the provenance record the request kept in its facts. A failure to embed leaves the bytes as
        they were (``media_delivery.attach_provenance`` already does)."""
        job = media_jobs.get(ctx.job_id, audience="internal", path=media_jobs.DB_PATH) or {}
        record = ((job.get("metadata") or {}).get(REQUEST_FACTS_KEY) or {}).get("provenance")
        if not isinstance(record, Mapping) or not record:
            return files
        if self.host.wire_id is not None:
            record = with_wire_id(record, self.host.wire_id(ctx.job_id))
        return [await asyncio.to_thread(media_delivery.attach_provenance, raw, _media_type_of(raw), record) for raw in files]

    async def _stages(self, ctx: RunContext, files: list[bytes], *, needs_cutout: bool = False) -> list[bytes]:
        """The binding's stages in order. ``background_fallback`` runs only when the cutout is needed and the engine's graph did
        not make it; then it runs first, and its files must carry transparency, or the run fails."""
        builtin = {"strip_audio": self._strip_audio, "attach_provenance": self._attach_provenance}
        names = [name for name in (ctx.pin.binding.get("result_processing") or ()) if name != CUTOUT_STAGE]
        if needs_cutout:
            names.insert(0, CUTOUT_STAGE)
        for stage in names:
            run = self.host.stages.get(stage) or builtin.get(stage)
            if run is None:
                if stage == CUTOUT_STAGE:
                    raise RunFailed("cutout_unavailable", "internal", "The background of this picture could not be removed.")
                raise RunFailed("stage_unavailable", "internal", "A result stage of this workflow is not available here.")
            files = await run(ctx, files)
            if stage == CUTOUT_STAGE and not all(_has_alpha(raw) for raw in files):
                raise RunFailed("cutout_unavailable", "internal", "The background of this picture could not be removed.")
        return files

    async def _store(self, ctx: RunContext, adapter: Any, files: list[bytes], name: str, limits: Any) -> ResultBundle:
        from gateway.engine_protocol.assets import served_media_type
        # A job whose cancel was asked for while its files were being fetched stores nothing: it ends canceled here.
        job = media_jobs.get(ctx.job_id, audience="internal", path=media_jobs.DB_PATH) or {}
        if (job.get("metadata") or {}).get("cancel_requested_at"):
            raise media_jobs.CancellationPending()
        store = result_bundle.LimitedAssetStore(adapter._assets, limits)
        saved = []
        for raw in files:
            content_type = served_media_type(None, raw[:16]) or "application/octet-stream"

            async def chunks(data: bytes = raw) -> AsyncIterator[bytes]:
                yield data

            saved.append(await store.save(chunks(), content_type=content_type, max_bytes=limits.max_asset_bytes, name=None))
        return ResultBundle({name: AssetsResult(tuple(saved))})

"""Core's durable submit/poll/fetch loop, over the versioned adapter interface.

The loop names no engine, adapter or node. It reads what the caller's runtime offers: an adapter, the connection for an
operation, the request for it and the poll timing. Three rules hold for every adapter:

- A submit whose reply is lost is never repeated: the job ends ``submission_unknown`` (no blind replay).
- A job's observed state only moves forward (``advance``); a terminal state stays put.
- Completion needs the engine's own terminal status, read by ``poll``. A push event can wake the loop early, and a
  push channel that closes or fails leaves the job to polling. Neither completes a job.

An adapter that declares interface version 2 is started with ``start``, which answers ``Pending`` (a job id, then the
loop above) or ``Completed`` (a whole result). The attempt marker is the same one for both: written just before the
call, and cleared only in the commit that publishes a ``Completed`` bundle (CODE_TO_DATA_S2 D3, D4). An adapter that
declares version 1 is submitted as before. ``run`` then returns the adapter's files for a version 1 adapter, and the
``ResultBundle`` that it published for a version 2 adapter that returned results; it returns ``None`` for a version 2
job that was already finished (a replay reads the stored result; the runner does not run it again).
"""
from __future__ import annotations

import asyncio
import contextlib

from gateway import media_jobs, result_bundle
from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION_V2
from gateway.engine_protocol.errors import BudgetExceeded, ProtocolError, TransportFailure, UpstreamError, retryable_poll_failure
from gateway.engine_protocol.types import Completed, JobRef, JobState, JsonResult, Pending, ResultBundle, ResultContext

TERMINAL = frozenset({JobState.succeeded, JobState.failed, JobState.cancelled})
_RANK = {JobState.pending: 0, JobState.running: 1}


def advance(current: JobState | None, observed: JobState) -> JobState:
    """The state after observing ``observed``. It never moves backwards, and a terminal state never changes."""
    if current is None:
        return observed
    if current in TERMINAL:
        return current
    if observed in TERMINAL:
        return observed
    return max(current, observed, key=_RANK.__getitem__)


class _Progress:
    """The job's observed state, kept with ``advance`` and written to the job's metadata each time it moves.

    It starts from the state already saved on the job, so a later run cannot move the job backwards.
    """

    def __init__(self, job_id: str, state: JobState | None = None) -> None:
        self.job_id = job_id
        self.state: JobState | None = state

    def observe(self, observed: JobState) -> None:
        moved = advance(self.state, observed)
        if moved is self.state:
            return
        self.state = moved
        media_jobs.update_status(self.job_id, media_jobs.STATUS_RUNNING,
                                 metadata_patch={'protocol_state': moved.value}, path=media_jobs.DB_PATH)


async def _follow(adapter, conn, ref, progress: _Progress, wake: asyncio.Event) -> None:
    """Read the engine's push channel. A terminal event only wakes the poll loop; it is not completion."""
    try:
        async for event in adapter.events(conn, ref):
            if event.state is None:
                continue
            if event.state in TERMINAL:
                wake.set()
            else:
                progress.observe(event.state)
    except Exception:  # noqa: BLE001 - a push channel that fails leaves the job to polling; it never fails the job
        return


async def _pause(wake: asyncio.Event, interval_s: float) -> None:
    try:
        await asyncio.wait_for(wake.wait(), timeout=interval_s)
    except TimeoutError:
        pass
    wake.clear()


def _saved_state(value) -> JobState | None:
    """The job state a previous run saved, or ``None`` when there is none or it is not a known state."""
    try:
        return JobState(value)
    except ValueError:
        return None


def _declares_v2(adapter) -> bool:
    """Whether the adapter declares interface version 2 (and so must implement ``start``). The declared number decides,
    never a name. A version 2 adapter without ``start`` is a defect of the adapter, refused before anything is sent."""
    version = getattr(adapter, 'interface_version', None)
    if not isinstance(version, int) or isinstance(version, bool) or version < ADAPTER_INTERFACE_VERSION_V2:
        return False
    if not callable(getattr(adapter, 'start', None)):
        raise ProtocolError(f'The adapter declares interface version {version} but has no start.', code='adapter_version_mismatch')
    return True


def _job_ref(outcome, v2: bool) -> JobRef:
    """The job reference of a start outcome. A version 1 ``submit`` returns the reference itself."""
    if not v2:
        return outcome
    if isinstance(outcome, Pending):
        return outcome.ref
    raise ProtocolError('The adapter answered start with something that is neither Pending nor Completed.', code='adapter_bad_outcome')


def _result_context(runtime) -> ResultContext:
    """The limits and the legacy setting that the caller's runtime says apply (trusted deployment config, never a
    manifest). A runtime that says nothing gets the code defaults."""
    context = getattr(runtime, 'result_context', None)
    return context() if context is not None else ResultContext(result_bundle.ResultLimits())


def _commit_bundle(runtime, job_id: str, bundle: ResultBundle, *, clear_marker: bool = True,
                   metadata: dict | None = None, **status_args) -> ResultBundle:
    """Stage the bundle and, in one transaction, publish it and end the job (D3, D4). The attempt marker is cleared in
    that same commit. Raises ``ResultRefused`` for a result that breaks a limit (nothing is published, the stage is
    aborted) and ``CancellationPending`` for a cancel that has not settled."""
    limits = _result_context(runtime).limits

    def stage(conn, attempt: int) -> None:
        for name, result in bundle.results.items():
            if isinstance(result, JsonResult):
                result_bundle.stage_json_in(conn, job_id, attempt, name, result.raw, limits=limits)
            else:
                items = [{'asset_ref': asset.id, 'media_type': asset.content_type, 'size_bytes': asset.size} for asset in result.assets]
                result_bundle.stage_assets_in(conn, job_id, attempt, name, items, limits=limits)

    patch = {**({'protocol_submitting': False} if clear_marker else {}), **(metadata or {})} or None
    result_bundle.complete_job(job_id, stage, limits=limits, metadata_patch=patch, path=media_jobs.DB_PATH, **status_args)
    return bundle


async def run(runtime, op, body, *, job_id, follow_events: bool = False):
    require_egress = getattr(runtime, 'require_egress', None)
    if require_egress is not None:
        require_egress()
    adapter = runtime.adapter
    v2 = _declares_v2(adapter)
    current = media_jobs.get(job_id, audience='internal', path=media_jobs.DB_PATH)
    if v2 and current is not None and media_jobs.is_terminal(str(current.get('status') or '')):
        # D3, first row: a finished job needs no recovery. Its marker or job id is not read, and nothing is polled again.
        return None
    conn = runtime.connection_for(op)
    interval_s, max_wait_s = runtime.poll_timing(op)
    metadata = (current or {}).get('metadata') or {}
    request = None
    saved = metadata.get('protocol_job')
    if saved:
        ref = JobRef(op, saved)
    else:
        if metadata.get('protocol_submitting'):
            # Submit is not automatically retryable after an ambiguous reply.
            raise ProtocolError('Submission was interrupted before its job id was saved. Inspect the upstream before retrying.',
                                code='submission_unknown')
        request = runtime.request(op, body)
        # Local checks run before the marker: a request the gateway refuses to send leaves no sign it was sent.
        validate = getattr(adapter, 'validate', None)
        if validate is not None:
            validate(conn, op, request)
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING,
                                 metadata_patch={'protocol_submitting':True}, path=media_jobs.DB_PATH)
        try:
            outcome = await adapter.start(conn, op, request) if v2 else await adapter.submit(conn, op, request)
        except TransportFailure as exc:
            # The engine may have started the job and its reply was lost: the marker stays, and no later run resubmits.
            raise ProtocolError('The engine did not answer the submission, so it may have started the job. Inspect the upstream before retrying.',
                                code='submission_unknown') from exc
        except UpstreamError as exc:
            if 400 <= exc.status < 500:
                # A 4xx refusal is definite: the engine did not start the job, so the marker is cleared. A 5xx keeps it.
                media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING,
                                         metadata_patch={'protocol_submitting': False}, path=media_jobs.DB_PATH)
            raise
        if isinstance(outcome, Completed):
            # The whole answer is in hand. It is staged and committed before the marker is cleared; a crash before the
            # commit leaves the marker, which a restart reads as submission_unknown (D3).
            return _commit_bundle(runtime, job_id, outcome.bundle)
        ref = _job_ref(outcome, v2)
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING,
                                 metadata_patch={'protocol_job':ref.id}, path=media_jobs.DB_PATH)
    progress = _Progress(job_id, _saved_state(metadata.get('protocol_state')))
    wake = asyncio.Event()
    follower = asyncio.ensure_future(_follow(adapter, conn, ref, progress, wake)) if follow_events else None
    last_error = None
    try:
        async with asyncio.timeout(max_wait_s):
            while True:
                current = media_jobs.get(job_id, audience='internal', path=media_jobs.DB_PATH) or {}
                if (current.get('metadata') or {}).get('cancel_requested_at'):
                    await adapter.cancel(conn, ref)
                    raise media_jobs.CancellationPending()
                try:
                    status = await adapter.poll(conn, ref)
                except (TransportFailure, UpstreamError) as exc:
                    if not retryable_poll_failure(exc):
                        raise
                    last_error = str(exc)
                else:
                    progress.observe(status.state)
                    if status.state == JobState.succeeded:
                        results = getattr(adapter, 'results', None) if v2 else None
                        if results is None:
                            return await adapter.fetch_assets(conn, ref, status)
                        # After a restart no request was built yet: build it now, so the pin is read and checked again.
                        request = request if request is not None else runtime.request(op, body)
                        bundle = await results(conn, ref, status, request, _result_context(runtime))
                        return _commit_bundle(runtime, job_id, bundle, clear_marker=False)
                    if status.state in (JobState.failed, JobState.cancelled):
                        # status.detail is what the engine said about the failure; it stays in the status and out of the error (decision D44).
                        raise ProtocolError('The upstream job did not succeed.', code='upstream_job_failed')
                await _pause(wake, interval_s)
    except TimeoutError:
        await adapter.cancel(conn, ref)
        raise BudgetExceeded('max_wait_s', 'The upstream job exceeded its wait limit.' + (f' Last poll failure: {last_error}' if last_error else '')) from None
    except asyncio.CancelledError:
        # A disconnected caller does not leave an untracked upstream running.
        await asyncio.shield(adapter.cancel(conn, ref))
        raise
    finally:
        if follower is not None:
            follower.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await follower

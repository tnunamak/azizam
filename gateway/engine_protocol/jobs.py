"""Core's durable submit/poll/fetch loop, over the versioned adapter interface.

The loop names no engine, adapter or node. It reads what the caller's runtime offers: an adapter, the connection for an
operation, the request for it and the poll timing. Three rules hold for every adapter:

- A submit whose reply is lost is never repeated: the job ends ``submission_unknown`` (no blind replay).
- A job's observed state only moves forward (``advance``); a terminal state stays put.
- Completion needs the engine's own terminal status, read by ``poll``. A push event can wake the loop early, and a
  push channel that closes or fails leaves the job to polling. Neither completes a job.
"""
from __future__ import annotations

import asyncio
import contextlib

from gateway import media_jobs
from gateway.engine_protocol.errors import BudgetExceeded, ProtocolError, TransportFailure, UpstreamError, retryable_poll_failure
from gateway.engine_protocol.types import JobRef, JobState

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


async def run(runtime, op, body, *, job_id, follow_events: bool = False):
    require_egress = getattr(runtime, 'require_egress', None)
    if require_egress is not None:
        require_egress()
    adapter, conn = runtime.adapter, runtime.connection_for(op)
    interval_s, max_wait_s = runtime.poll_timing(op)
    current = media_jobs.get(job_id, audience='internal', path=media_jobs.DB_PATH)
    metadata = (current or {}).get('metadata') or {}
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
            ref = await adapter.submit(conn, op, request)
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
                        return await adapter.fetch_assets(conn, ref, status)
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

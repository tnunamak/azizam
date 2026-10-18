"""Core's durable submit/poll/fetch loop, over the versioned adapter interface."""
from __future__ import annotations

import asyncio

from gateway import media_jobs
from gateway.engine_protocol.errors import BudgetExceeded, ProtocolError, TransportFailure, UpstreamError, retryable_poll_failure
from gateway.engine_protocol.types import JobRef, JobState


async def run(runtime, op, body, *, job_id):
    adapter, conn = runtime.adapter, runtime.connection
    spec = runtime.target.description.operations[op].job
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
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING,
                                 metadata_patch={'protocol_submitting':True}, path=media_jobs.DB_PATH)
        ref = await adapter.submit(conn, op, runtime.request(op, body))
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING,
                                 metadata_patch={'protocol_job':ref.id}, path=media_jobs.DB_PATH)
    last_error = None
    try:
        async with asyncio.timeout(spec.poll.max_wait_s):
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
                    await asyncio.sleep(spec.poll.interval_s)
                    continue
                if status.state == JobState.succeeded:
                    return await adapter.fetch_assets(conn, ref, status)
                if status.state in (JobState.failed, JobState.cancelled):
                    raise ProtocolError(status.detail or 'The upstream job did not succeed.', code='upstream_job_failed')
                await asyncio.sleep(spec.poll.interval_s)
    except TimeoutError:
        await adapter.cancel(conn, ref)
        raise BudgetExceeded('max_wait_s', 'The upstream job exceeded its wait limit.' + (f' Last poll failure: {last_error}' if last_error else '')) from None
    except asyncio.CancelledError:
        # A disconnected caller does not leave an untracked upstream running.
        await asyncio.shield(adapter.cancel(conn, ref))
        raise

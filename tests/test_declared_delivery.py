"""Delivery is an optional consumer of an already-published result."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway import declared_operation_job, media_jobs, result_bundle
from gateway.engine_protocol.types import CanonicalRequest, Completed, JsonResult, ResultBundle, ResultContext


@pytest.mark.parametrize('fails', [False, True], ids=['answer', 'delivery-fails'])
def test_delivery_observes_published_success_and_cannot_undo_it(tmp_path, monkeypatch, fails):
    path = tmp_path / 'jobs.db'
    media_jobs.init_db(path)
    monkeypatch.setattr(media_jobs, 'DB_PATH', path)
    job = media_jobs.create(media_jobs.MediaJob(intent='specimen.render'), path=path)
    bundle = ResultBundle({'value': JsonResult(b'{"ready":true}')})
    seen = []

    async def start(conn, op, request):
        return Completed(bundle)

    async def deliver(published):
        assert published == bundle
        assert media_jobs.get(job.job_id, path=path)['status'] == 'succeeded'
        assert result_bundle.bundle_summary(job.job_id, path=path) is not None
        seen.append(published)
        if fails:
            raise ValueError('delivery refused')
        return b'client answer'

    runtime = SimpleNamespace(adapter=SimpleNamespace(interface_version=2, start=start),
                              connection_for=lambda op: None, poll_timing=lambda op: (0, 1),
                              request=lambda op, body: CanonicalRequest(op=op, body=body),
                              result_context=lambda: ResultContext(result_bundle.ResultLimits()), deliver=deliver)
    call = declared_operation_job.run(runtime, 'specimen.render', {}, job_id=job.job_id)
    if fails:
        with pytest.raises(ValueError, match='delivery refused'):
            asyncio.run(call)
    else:
        assert asyncio.run(call) == b'client answer'
    assert seen == [bundle]
    assert media_jobs.get(job.job_id, path=path)['status'] == 'succeeded'
    assert result_bundle.bundle_summary(job.job_id, path=path) is not None

"""Delivery is an optional consumer of an already-published result."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway import declared_operation_job, media_jobs, result_bundle
from gateway.engine_protocol.types import CanonicalRequest, Completed, JsonResult, ResultBundle, ResultContext


@pytest.mark.parametrize('fails', [False, True, 'typed'], ids=['answer', 'delivery-fails', 'typed-refusal'])
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
        if fails == 'typed':
            raise result_bundle.ResultRefused('asset_too_large', 'delivery refused')
        if fails:
            raise ValueError('delivery refused')
        return b'client answer'

    runtime = SimpleNamespace(adapter=SimpleNamespace(interface_version=2, start=start),
                              connection_for=lambda op: None, poll_timing=lambda op: (0, 1),
                              request=lambda op, body: CanonicalRequest(op=op, body=body),
                              result_context=lambda: ResultContext(result_bundle.ResultLimits()), deliver=deliver)
    call = declared_operation_job.run(runtime, 'specimen.render', {}, job_id=job.job_id)
    if fails:
        kind = result_bundle.ResultRefused if fails == 'typed' else ValueError
        with pytest.raises(kind, match='delivery refused') as caught:
            asyncio.run(call)
        if fails == 'typed':
            assert caught.value.code == 'asset_too_large'
    else:
        assert asyncio.run(call) == b'client answer'
    assert seen == [bundle]
    assert media_jobs.get(job.job_id, path=path)['status'] == 'succeeded'
    assert result_bundle.bundle_summary(job.job_id, path=path) is not None


def test_multipart_store_refusal_keeps_its_type_and_code_through_declared_run(tmp_path, monkeypatch):
    import httpx
    from tests.support.fake_upstream import HandlerTransport

    from gateway import multipart_adapter
    from gateway.mesh_manifest import ImageAsset
    from tests.media_test_bytes import glb_bytes, png_bytes

    path = tmp_path / 'jobs.db'
    media_jobs.init_db(path)
    monkeypatch.setattr(media_jobs, 'DB_PATH', path)
    job = media_jobs.create(media_jobs.MediaJob(intent='specimen.render'), path=path)
    refusal = result_bundle.ResultRefused('asset_too_large', 'result exceeds the configured limit')
    stored = []
    content = glb_bytes(b'result')

    def store_output(caller, output):
        stored.append(output.data)
        raise refusal

    runner = multipart_adapter.runner(SimpleNamespace(store_output=store_output))
    ctx = SimpleNamespace(job_id=job.job_id, caller=None, media={},
                          pin=SimpleNamespace(contract={'outputs': {'shape': {'media_type': 'model/gltf-binary'}}}))

    async def go():
        async with httpx.AsyncClient(transport=HandlerTransport(
            lambda request: httpx.Response(200, content=content))) as client:
            prepared = multipart_adapter._MultipartRun(
                runner, ctx, client, 'http://engine.test/render', 'source',
                ImageAsset(png_bytes(), 'input.png', 'image/png'), {}, 1, client.aclose)
            await declared_operation_job.run(prepared, 'specimen.render', {}, job_id=job.job_id)

    with pytest.raises(result_bundle.ResultRefused) as caught:
        asyncio.run(go())
    assert caught.value is refusal
    assert caught.value.code == 'asset_too_large'
    assert stored == [content]
    assert result_bundle.bundle_summary(job.job_id, path=path) is None


@pytest.mark.parametrize('code', [
    'submission_unknown', 'service_unavailable', 'resource_unavailable',
    'input_unavailable', 'result_not_kept', 'upstream_timeout',
    'upstream_unavailable', 'upstream_error',
])
def test_start_refusal_keeps_code_and_client_message(tmp_path, monkeypatch, code):
    from gateway.multipart_adapter import RunError

    path = tmp_path / 'jobs.db'
    media_jobs.init_db(path)
    monkeypatch.setattr(media_jobs, 'DB_PATH', path)
    job = media_jobs.create(media_jobs.MediaJob(intent='specimen.render'), path=path)
    refusal = RunError(code, 'upstream', client_message='The service cannot run this request.')

    async def start(conn, op, request):
        raise refusal

    runtime = SimpleNamespace(adapter=SimpleNamespace(interface_version=2, start=start),
                              connection_for=lambda op: None, poll_timing=lambda op: (0, 1),
                              request=lambda op, body: CanonicalRequest(op=op, body=body))
    with pytest.raises(RunError) as caught:
        asyncio.run(declared_operation_job.run(runtime, 'specimen.render', {}, job_id=job.job_id))
    assert caught.value is refusal
    assert caught.value.code == code
    assert caught.value.client_message == 'The service cannot run this request.'
    assert result_bundle.bundle_summary(job.job_id, path=path) is None

"""Publication stays successful when the audio delivery hook fails.

The separate callback probe pins why delivery must follow publication.
The same result bytes go through the existing storage and stripping functions.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import proxy
from gateway import declared_operation_job, media_delivery, media_ingestion, media_jobs, media_metadata, receipt_store
from gateway.engine_protocol.types import CanonicalRequest
from gateway.receipts import RouteReceipt
from tests.test_audio_route_selectors import BODY, NOISY, build


def test_delivery_failure_after_storage_changes_publication_with_linked_declared_job(tmp_path):
    rig, made = build(tmp_path, NOISY)
    stored_inputs = []
    original_store = media_ingestion.store_audio_response

    def record_store(content, media_type, receipt, **kwargs):
        stored_inputs.append((content, media_type))
        return original_store(content, media_type, receipt, **kwargs)

    def refuse_delivery(*args, **kwargs):
        raise media_metadata.MediaMetadataError('injected delivery rewrite failure')

    made.set(media_ingestion, 'store_audio_response', record_store)
    made.set(media_delivery, 'deliver_bytes', refuse_delivery)
    try:
        # The route publishes the stored result, then attempts delivery rewriting.
        response = asyncio.run(made.call('POST', '/v1/audio/generate', label='rewrite failure', json_body=BODY))
        assert response.status_code == 502
        assert json.loads(response.content)['error']['message'] == 'Could not prepare audio for delivery'
        old_receipt = receipt_store.get(response.headers['x-request-id'], path=receipt_store.DB_PATH)
        assert old_receipt['status'] == 'failed'
        old_job = media_jobs.get(old_receipt['job_id'], audience='internal', path=media_jobs.DB_PATH)
        assert old_job['status'] == 'succeeded'
        assert len(old_job['assets']) == 1
        assert len(stored_inputs) == 1
        content, media_type = stored_inputs[0]

        # A declared run owns an existing job. The same helper must not create a
        # second job (index_receipt_assets deliberately skips a linked receipt).
        job = media_jobs.MediaJob(intent=old_job['intent'], request_id='audio-boundary-probe',
                                  status=media_jobs.STATUS_RUNNING)
        media_jobs.create(job, path=media_jobs.DB_PATH)
        receipt = RouteReceipt(protocol='openai.audio', intent=old_job['intent'],
                               path='/v1/audio/generate', request_id=job.request_id, job_id=job.job_id)

        async def read_results(conn, op, request):
            await asyncio.to_thread(original_store, content, media_type, receipt, store_asset=proxy.store_asset_bytes)
            # This is the exact old helper and the same result bytes. The failure
            # happens before a Completed bundle can reach core's commit.
            await asyncio.to_thread(media_delivery.deliver_bytes, content, '.wav', 'none')
            pytest.fail('delivery must refuse before returning a result bundle')

        runtime = SimpleNamespace(
            adapter=SimpleNamespace(interface_version=2, start=read_results),
            connection_for=lambda op: None,
            request=lambda op, body: CanonicalRequest(op=op, body=body),
            poll_timing=lambda op: (0, 1),
        )
        with pytest.raises(media_metadata.MediaMetadataError, match='injected delivery'):
            asyncio.run(declared_operation_job.run(runtime, old_job['intent'], BODY, job_id=job.job_id))
        new_job = media_jobs.get(job.job_id, audience='internal', path=media_jobs.DB_PATH)
        assert new_job['status'] == 'running'
        assert new_job['assets'] == []
        assert not new_job.get('results')
        print('Old route: HTTP 502, failed receipt, succeeded Library job with one asset.')
        print('Declared result callback: rewrite raises before completion; linked job remains running with no assets or results.')
    finally:
        rig.__exit__(None, None, None)

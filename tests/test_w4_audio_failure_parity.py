"""Failure/publication comparisons against frozen fd30cbfca handlers.

The owner-approved synchronous limit refusal is explicit; empty storage keeps
the original successful byte response.
"""
import asyncio
from pathlib import Path

import pytest

import proxy
from gateway import media_jobs, receipt_store, media_delivery, media_metadata
from gateway.engine_protocol.errors import TransportFailure, UpstreamError
from tests import test_adapter_differential as base
from tests.support.adapter_differential_rig import GatewayRig
from tests.support.config_snapshot import publish_section

SOURCE = Path(__file__).parent / 'fixtures/w4_audio_fd30cbfca.py'
BODY = {'prompt': 'a door creaks', 'model': 'tangoflux', 'duration': 4}


def old_handlers(route_handler=None):
    namespace = dict(vars(proxy))
    exec(compile(SOURCE.read_text(), str(SOURCE), 'exec'), namespace)
    if route_handler is not None:
        namespace['proxy_audio_generate'] = route_handler
    return namespace['proxy_audio_generate'], namespace['_run_audio_media_job']


def observe(tmp_path, old, mode, case, capsys):
    with GatewayRig('audio-failure-parity', tmp_path) as rig:
        base._audio_comfy(rig, strict=False)
        rig.set(proxy, '_schedule_audio_media_job', lambda job_id: rig.scheduled.append(job_id) or True)
        counters = {'submit': 0, 'history': 0, 'stored': 0}
        during = []
        publication = []
        waiting_for_engine = asyncio.Event()
        job_id = None
        real_graph = proxy._graph
        real_store = proxy.store_asset_bytes
        real_update = media_jobs.update_status

        def store(content, **options):
            counters['stored'] += 1
            if case == 'cancel_pending':
                real_update(job_id, 'running', metadata_patch={'cancel_requested_at': 1}, path=media_jobs.DB_PATH)
            return real_store(content, **options)
        rig.set(proxy, 'store_asset_bytes', store)

        def update(*args, **kwargs):
            row = real_update(*args, **kwargs)
            if row and row['status'] == 'succeeded':
                visible = media_jobs.get(row['job_id'], audience='client', path=media_jobs.DB_PATH)
                if visible:
                    publication.append((visible['status'], len(visible['assets'])))
            return row
        rig.set(media_jobs, 'update_status', update)

        def graph(*args, **kwargs):
            session = real_graph(*args, **kwargs)
            class Wire:
                async def submit(self, *args, **kwargs):
                    counters['submit'] += 1
                    if case.startswith('submit_'):
                        raise TransportFailure(case.removeprefix('submit_'), 'injected submit failure')
                    return await session.submit(*args, **kwargs)

                async def history(self, *args, **kwargs):
                    counters['history'] += 1
                    if case.startswith('history_'):
                        assert counters['history'] == 1, 'old route did not retry this history error'
                        kind = case.removeprefix('history_')
                        if kind == 'upstream':
                            raise UpstreamError(503, 'injected history refusal')
                        raise TransportFailure(kind, 'injected history failure')
                    if case == 'empty_output':
                        from gateway.engine_protocol.httpx_wire import reply_from
                        import httpx
                        return reply_from(httpx.Response(200, json={'p1': {'status': {'status_str': 'success'}, 'outputs': {}}}))
                    if case == 'client_disconnect':
                        waiting_for_engine.set()
                        await asyncio.Future()
                    if case == 'sync_cancel_pending':
                        raise media_jobs.CancellationPending('injected cancellation')
                    if case == 'shutdown':
                        raise asyncio.CancelledError()
                    if case in ('generation_timeout', 'poll_limit_600'):
                        from gateway.engine_protocol.httpx_wire import reply_from
                        import httpx
                        return reply_from(httpx.Response(200, json={}))
                    if case == 'pending_visibility' and counters['history'] == 1:
                        during.append((media_jobs.active_count(app_id='app_test', intents=['audio.generate'], path=media_jobs.DB_PATH),
                                       len(media_jobs.recent(path=media_jobs.DB_PATH))))
                    return await session.history(*args, **kwargs)

                async def view(self, *args, **kwargs):
                    response = await session.view(*args, **kwargs)
                    if case in ('unsupported_type', 'opus_without_ogg'):
                        from gateway.engine_protocol.httpx_wire import reply_from
                        import httpx
                        media_type = 'application/octet-stream' if case == 'unsupported_type' else 'audio/opus'
                        return reply_from(httpx.Response(200, content=response.raw, headers={'content-type': media_type}))
                    return response
            return Wire()
        rig.set(proxy, '_graph', graph)
        if case == 'generation_timeout' and not old:
            # Exercise the core deadline winning the race against the 600-poll counter.
            rig.set(proxy._PreparedAudioGeneration, 'poll_timing', lambda self, op: (0, 0.001))
        if case == 'delivery_failure':
            def refuse(*args, **kwargs):
                raise media_metadata.MediaMetadataError('rewrite refused')
            rig.set(media_delivery, 'deliver_bytes', refuse)
            rig.set(proxy.log_scrub, 'describe_error', lambda error: 'scrubbed rewrite failure')
        if case == 'limit':
            publish_section(rig.monkeypatch, 'workflow_execution', {'result_limits': {'max_asset_bytes': 10}})
        if case == 'empty':
            if mode == 'sync':
                from gateway import media_ingestion
                rig.set(media_ingestion, 'store_audio_response', lambda *args, **kwargs: [])
            else:
                rig.set(proxy.audio_media_jobs, 'audio_artifacts_from_result', lambda *args, **kwargs: [])

        async def cancelled_route(*args, **kwargs):
            raise media_jobs.CancellationPending('injected route cancellation')

        if case == 'get_before_delivery':
            deliver = proxy._PreparedAudioGeneration.deliver
            async def read_before_delivery(prepared, bundle):
                answer = await rig.call('GET', f'/gateway/v1/media/jobs/{prepared.job_id}', label='read committed job')
                assert answer.status_code == 200, answer.text
                document = answer.json()
                assert document['status'] == 'succeeded' and document['assets'], document
                during.append('succeeded with assets')
                return await deliver(prepared, bundle)
            rig.set(proxy._PreparedAudioGeneration, 'deliver', read_before_delivery)

        if old:
            handler, worker = old_handlers(cancelled_route if case == 'cancel_during_route' else None)
            rig.set(proxy, "proxy_audio_generate", handler)
        else:
            worker = proxy._run_audio_media_job
            if case == 'cancel_during_route':
                rig.set(proxy, 'proxy_audio_generate', cancelled_route)

        async def drive():
            nonlocal job_id
            if mode == 'queued':
                response = await rig.call('POST', '/gateway/v1/media/jobs', label='accept', unordered_json=True,
                                          json_body={'intent': 'audio.generate', 'request': BODY})
                assert response.status_code == 202
                job_id = response.json()['job_id']
                try:
                    await worker(job_id)
                except asyncio.CancelledError:
                    assert case == 'shutdown'
                return response
            try:
                return await rig.call('POST', '/v1/audio/generate', label=case, json_body=BODY,
                                      cancel_when=waiting_for_engine.wait if case == 'client_disconnect' else None)
            except asyncio.CancelledError:
                assert case == 'shutdown'
                return None

        capsys.readouterr()
        response = asyncio.run(drive())
        logs = capsys.readouterr().out
        receipts = receipt_store.recent(audience='internal', path=receipt_store.DB_PATH)
        receipt = next(row for row in receipts if row['path'] == ('/gateway/v1/media/jobs' if mode == 'queued' else '/v1/audio/generate'))
        visible = media_jobs.get(job_id, path=media_jobs.DB_PATH) if job_id else next(iter(media_jobs.recent(path=media_jobs.DB_PATH)), None)
        if (response is not None and mode == 'sync' and response.status_code >= 400
                and response.headers.get('content-type', '').startswith('application/json')):
            error = response.json().get('error') or {}
        else:
            error = {}
        summary = {'status': response.status_code if response else None, 'code': error.get('code'),
                   'job_status': visible['status'] if visible else None, 'receipt_status': receipt['status'],
                   'job_error': visible.get('error') if visible else None,
                   'error_info': {key: visible.get(key) for key in media_jobs.ERROR_FIELDS} if visible else None}
        if case.startswith(('submit_', 'history_')) or case in ('generation_timeout', 'poll_limit_600'):
            summary['message'] = error.get('message')
        if case in ('limit', 'empty', 'unsupported_type', 'opus_without_ogg'):
            summary['stored'] = counters['stored']
        if case == 'pending_visibility':
            summary['during'] = during
        if case == 'get_before_delivery':
            assert during == ['succeeded with assets']
        if case == 'poll_limit_600':
            assert counters['history'] == 600
        if case in ('empty', 'unsupported_type', 'opus_without_ogg') and mode == 'sync':
            summary['wire'] = rig.calls[-1]['response']
        if case in ('cancel_during_route', 'cancel_pending', 'poll_limit_600'):
            summary['receipt_http_status'] = receipt['metadata']['status_code']
        if case == 'publication_atomic':
            assert all(count > 0 for status, count in publication), publication
            assert visible and visible['status'] == 'succeeded' and visible['assets']
        if case in ('success', 'delivery_failure'):
            summary['submitted_log'] = 'Audio job submitted: workflow=tangoflux id=audio_p1' in logs
            if case == 'delivery_failure':
                summary['rewrite_log'] = 'audio metadata rewrite failed: scrubbed rewrite failure' in logs
        if case.startswith('history_'):
            assert counters['history'] == 1
        if case in ('shutdown', 'sync_cancel_pending', 'client_disconnect') and mode == 'sync':
            assert media_jobs.unfinished(path=media_jobs.DB_PATH) == []
            assert media_jobs.get_by_request_id(receipt['request_id'], audience='internal', include_deleted=True,
                                                path=media_jobs.DB_PATH) is None
        return summary


@pytest.mark.parametrize('mode', ['sync', 'queued'])
@pytest.mark.parametrize('case', ['submit_unreachable', 'submit_timeout', 'history_unreachable', 'history_timeout',
                                  'history_upstream', 'generation_timeout', 'shutdown', 'publication_atomic',
                                  'success', 'delivery_failure', 'empty_output'])
def test_failure_and_publication_match_fd30cbfca(tmp_path, mode, case, capsys):
    old = observe(tmp_path / 'old', True, mode, case, capsys)
    new = observe(tmp_path / 'new', False, mode, case, capsys)
    assert new == old


def test_pending_sync_row_matches_old_admission_and_listing_visibility(tmp_path, capsys):
    old = observe(tmp_path / 'old', True, 'sync', 'pending_visibility', capsys)
    new = observe(tmp_path / 'new', False, 'sync', 'pending_visibility', capsys)
    assert old['during'] == [(0, 0)]
    assert new == old


def test_cancel_pending_reaches_the_old_settle_path(tmp_path, capsys):
    old = observe(tmp_path / 'old', True, 'queued', 'cancel_pending', capsys)
    new = observe(tmp_path / 'new', False, 'queued', 'cancel_pending', capsys)
    assert old['job_status'] == 'canceled'
    assert old['job_error'] == 'Canceled by requester'
    assert old['receipt_http_status'] == 200
    assert new == old


@pytest.mark.parametrize('case', ['limit', 'empty'])
def test_queued_result_refusals_match_old_and_sync_refusals_leave_no_asset(tmp_path, case, capsys):
    old = observe(tmp_path / 'old', True, 'queued', case, capsys)
    new = observe(tmp_path / 'new', False, 'queued', case, capsys)
    assert new == old
    baseline = observe(tmp_path / 'sync-old', True, 'sync', case, capsys)
    refused = observe(tmp_path / 'sync-new', False, 'sync', case, capsys)
    assert baseline['status'] == 200
    if case == 'empty':
        assert refused == baseline
    else:
        # Owner decision: synchronous result limits refuse before any asset is stored.
        assert refused['status'] == 502 and refused['receipt_status'] == 'failed'
        assert refused['code'] == 'result_refused'
        assert refused['job_status'] is None and refused['stored'] == 0


@pytest.mark.parametrize('mode', ['sync', 'queued'])
def test_600_poll_timeout_matches_the_frozen_handler(tmp_path, mode, capsys):
    old = observe(tmp_path / 'old', True, mode, 'poll_limit_600', capsys)
    new = observe(tmp_path / 'new', False, mode, 'poll_limit_600', capsys)
    assert new == old
    if mode == 'sync':
        assert old['status'] == 504 and old['message'] == 'Audio generation timeout'
    else:
        assert old['receipt_http_status'] == 504 and old['job_status'] == 'failed'
        assert old['job_error'] == 'The backend did not finish this job in time.'


@pytest.mark.parametrize('case', ['unsupported_type', 'opus_without_ogg'])
def test_unindexed_sync_audio_keeps_the_original_bytes_and_200(tmp_path, case, capsys):
    old = observe(tmp_path / 'old', True, 'sync', case, capsys)
    new = observe(tmp_path / 'new', False, 'sync', case, capsys)
    assert old['status'] == 200 and old['stored'] == 0 and old['job_status'] is None
    assert new == old


def test_cancelled_sync_call_discards_its_owned_row(tmp_path, capsys):
    observe(tmp_path, False, 'sync', 'sync_cancel_pending', capsys)


def test_cancelled_queued_route_keeps_the_original_500_receipt(tmp_path, capsys):
    old = observe(tmp_path / 'old', True, 'queued', 'cancel_during_route', capsys)
    new = observe(tmp_path / 'new', False, 'queued', 'cancel_during_route', capsys)
    assert old['receipt_http_status'] == new['receipt_http_status'] == 500
    assert old['receipt_status'] == new['receipt_status'] == 'failed'
    # Native settlement fixes the old swallowed cancellation; the receipt still matches.
    assert new['job_status'] == 'canceled' and new['job_error'] == 'Canceled by requester'


@pytest.mark.parametrize('mode', ['sync', 'queued'])
def test_get_between_commit_and_delivery_includes_assets(tmp_path, mode, capsys):
    observe(tmp_path, False, mode, 'get_before_delivery', capsys)


def test_disconnected_sync_request_leaves_no_hidden_row(tmp_path, capsys):
    old = observe(tmp_path / 'old', True, 'sync', 'client_disconnect', capsys)
    new = observe(tmp_path / 'new', False, 'sync', 'client_disconnect', capsys)
    assert new == old

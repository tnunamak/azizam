"""Reproductions of the independent R16 review findings."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.responses import JSONResponse

import proxy
from gateway import media_jobs, me_make, resource_admission_http
from gateway.admin_api import _RouteError, _validate_media_job_admission_overrides
from test_decision_routing import BODY, runtime
from test_dispatch_admission import dispatch_env  # noqa: F401


@pytest.mark.parametrize('cancel_before_start', [False, True])
def test_make_database_cancel_always_settles(tmp_path, cancel_before_start):
    path = tmp_path / 'jobs.db'
    media_jobs.init_db(path)
    job = media_jobs.create(media_jobs.MediaJob(intent='image.generate', metadata={'mode': me_make.JOB_MODE}), path=path)
    job_id = job.job_id
    payloads = {job_id: {'body': {}}}

    class Runtime:
        load_payload = lambda self, key: payloads.get(key)
        drop_payload = lambda self, key: payloads.pop(key, None)

        async def execute(self, payload):
            media_jobs.request_cancel(job_id, path=path)
            return [{'asset_id': 'img_result'}]

    if cancel_before_start:
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, path=path)
        media_jobs.request_cancel(job_id, path=path)
    asyncio.run(me_make.run_job(job_id, runtime=Runtime(), jobs_path=path)())
    result = media_jobs.get(job_id, path=path)
    assert result['status'] == media_jobs.STATUS_CANCELED
    assert result['assets'] == []
    assert not payloads


@pytest.mark.parametrize('recover_while_queued', [False, True])
def test_recovery_skips_live_and_scheduled_make_jobs(tmp_path, monkeypatch, recover_while_queued):
    path = tmp_path / 'jobs.db'
    media_jobs.init_db(path)
    monkeypatch.setattr(media_jobs, 'DB_PATH', path)
    monkeypatch.setattr(proxy, '_image_media_job_scheduled', set())
    monkeypatch.setattr(proxy, '_image_media_job_tasks', set())
    payloads = {}
    rt = proxy._SessionMakeRuntime()
    monkeypatch.setattr(rt, 'load_payload', lambda key: payloads.get(key))
    monkeypatch.setattr(rt, 'drop_payload', lambda key: payloads.pop(key, None))

    async def scenario():
        monkeypatch.setattr(proxy, '_image_media_job_semaphore', asyncio.Semaphore(1))
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def execute(payload):
            calls.append(payload)
            started.set()
            await release.wait()
            return [{'asset_id': 'img_result'}]
        monkeypatch.setattr(rt, 'execute', execute)
        job = media_jobs.create(media_jobs.MediaJob(intent='image.generate', metadata={'mode': me_make.JOB_MODE}), path=path)
        key = job.job_id
        payloads[key] = {'body': {}}
        assert me_make.resume_jobs(runtime=rt, jobs_path=path) == 1
        try:
            if recover_while_queued:
                assert me_make.resume_jobs(runtime=rt, jobs_path=path) == 0
            await asyncio.wait_for(started.wait(), 2)
            assert me_make.resume_jobs(runtime=rt, jobs_path=path) == 0
            assert media_jobs.get(key, path=path)['status'] == 'running'
            assert key in payloads
        finally:
            release.set()
            await asyncio.gather(*proxy._image_media_job_tasks)
        assert media_jobs.get(key, path=path)['status'] == 'succeeded'
        assert len(calls) == 1
        assert me_make.resume_jobs(runtime=rt, jobs_path=path) == 0
    asyncio.run(scenario())


def test_denied_decision_fallback_never_activates():
    ensure = AsyncMock(return_value=None)
    rt = runtime(ensure=ensure)
    rt.admit = lambda auth, service: JSONResponse({'error': 'denied'}, status_code=503) if service == 'small' else None
    real = httpx.AsyncClient
    posts = []
    def handler(request):
        posts.append(request.url.port)
        return httpx.Response(503, json={'error': 'busy'})
    with patch('gateway.decision_routing.httpx.AsyncClient', lambda **kw: real(transport=httpx.MockTransport(handler))):
        result = asyncio.run(rt.dispatch(None, None, {**BODY, 'model': 'decide-quality'}, 'r'))
    assert result.status_code == 503
    assert [call.args[0] for call in ensure.call_args_list] == ['large']
    assert posts == [18092]


def test_decision_rechecks_policy_after_readiness():
    ready = False
    async def ensure(service):
        nonlocal ready
        ready = True
    rt = runtime(ensure=ensure)
    admissions = []
    def admit(auth, service):
        admissions.append(ready)
        return JSONResponse({'error': 'denied'}, status_code=503) if ready else None
    rt.admit = admit
    with patch('gateway.decision_routing.httpx.AsyncClient', side_effect=AssertionError('must not send')):
        result = asyncio.run(rt.dispatch(None, None, BODY, 'r'))
    assert result.status_code == 503
    assert admissions == [False, True]


@pytest.mark.parametrize('cfg', [{'max_queue_depth': 1}, {'max_app_concurrency': {'mine': 1}}])
@pytest.mark.parametrize('path,body', [('/v1/chat/completions', {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}]}), ('/v1/embeddings', {'model': 'test', 'input': 'hi'})])
def test_direct_traffic_ignores_untracked_limits(dispatch_env, monkeypatch, cfg, path, body):
    client, calls = dispatch_env
    monkeypatch.setattr(proxy, 'MEDIA_JOB_ADMISSION_CFG', cfg)
    monkeypatch.setattr(proxy, '_resolve_request_target', lambda *a, **kw: __import__('types').SimpleNamespace(logical_service_id='selected'))
    monkeypatch.setitem(proxy.config['backends'], 'selected', {'enabled': True})
    # This path has an app identity but no tracked queue or active-request counter.
    from gateway.receipts import RouteReceipt
    receipt = RouteReceipt(protocol='openai', intent='chat.generate', app_id='mine')
    assert proxy._dispatch_admission_response('chat.generate', resource_class='chat', service_available=True, receipt=receipt) is None
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert calls


def test_media_app_limit_uses_stored_occupancy(dispatch_env, monkeypatch):
    monkeypatch.setattr(proxy, 'MEDIA_JOB_ADMISSION_CFG', {'max_app_concurrency': {'mine': 1}})
    from gateway.receipts import RouteReceipt
    receipt = RouteReceipt(protocol='test', intent='image.generate', app_id='mine')
    assert proxy._media_job_admission_response('image.generate', receipt=receipt) is None
    job = media_jobs.create(media_jobs.MediaJob(intent='image.generate', app_id='mine'), path=media_jobs.DB_PATH)
    refused = proxy._media_job_admission_response('image.generate', receipt=receipt)
    assert refused.status_code == 503
    assert 'app_concurrency_full' in json.loads(refused.body)['admission']['reason_codes']
    assert proxy._media_job_admission_response('image.generate', count_queue=False, receipt=receipt) is None
    media_jobs.update_status(job.job_id, 'failed', path=media_jobs.DB_PATH)
    assert proxy._media_job_admission_response('image.generate', receipt=receipt) is None


@pytest.mark.parametrize('value', [0.5, -0.5, 1.9, -1, True, None, '2', float('inf'), float('nan')])
def test_app_limit_rejects_non_integer_input(value):
    with pytest.raises(_RouteError) as error:
        _validate_media_job_admission_overrides({'max_app_concurrency': {'mine': value}})
    assert error.value.status_code == 400
    assert 'non-negative integers' in error.value.message


@pytest.mark.parametrize('protocol', ['ollama', 'openai', 'anthropic'])
def test_admission_translation_is_idempotent(protocol):
    decision = resource_admission_http.advise_route_admission(intent='chat.generate', service_available=False)
    body = resource_admission_http.admission_http_response(decision).body
    once = resource_admission_http.admission_protocol_body(body, protocol)
    assert resource_admission_http.admission_protocol_body(once, protocol) == once

"""Dispatch admission and exact default-response differential against 803f8350.

Only backend transports are fake. Public routes, protocol translations, routing,
receipts and streaming serialization run in process. The baseline JSON is captured
by this same test on the pre-gate tree (see its source_commit field).
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

import proxy
from gateway import media_jobs, receipt_store, receipts, media_ingestion
from gateway.policy import AppProfile
from test_anthropic_schema_conformance import chat_completion, chunk, sse

CASES = [
    ('chat', '/v1/chat/completions', {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}]}),
    ('chat-stream', '/v1/chat/completions', {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True}),
    ('anthropic', '/v1/messages', {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 10}),
    ('anthropic-stream', '/v1/messages', {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 10, 'stream': True}),
    ('ollama-chat', '/api/chat', {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': False}),
    ('ollama-generate', '/api/generate', {'model': 'test', 'prompt': 'hi', 'stream': False}),
    ('ollama-stream', '/api/generate', {'model': 'test', 'prompt': 'hi', 'stream': True}),
    ('embeddings', '/v1/embeddings', {'model': 'test', 'input': 'hi'}),
    ('ollama-embed', '/api/embed', {'model': 'test', 'input': 'hi'}),
    ('decisions', '/v1/decisions', {'model': 'test', 'input': 'hi'}),
    ('systemone', '/v1/systemone', {'model': 'test', 'state': 'hi', 'questions': {'q': {'type': 'choice', 'criteria': {'a': 'A', 'b': 'B'}}}}),
    ('image', '/v1/images/generations', {'model': 'flux2-klein-4b', 'prompt': 'hi', 'response_format': 'b64_json'}),
    ('speech', '/v1/audio/speech', {'model': 'tts-1', 'input': 'hi', 'voice': 'alloy', 'response_format': 'wav'}),
]


@pytest.fixture
def dispatch_env(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, 'AUTH_ENABLED', False)
    monkeypatch.setattr(proxy, 'MEDIA_JOB_ADMISSION_CFG', {})
    monkeypatch.setattr(proxy, '_operator_override_docs', lambda: {'service_availability': {}, 'media_job_admission': {}})
    monkeypatch.setattr(proxy, 'PROXY_PORT', 19999)
    monkeypatch.setattr(proxy, 'COMFY_ENABLED', True)
    monkeypatch.setattr(proxy, 'TTS_ENABLED', True)
    monkeypatch.setattr(proxy, 'IMAGE_BACKGROUND_REMOVAL_ENABLED', False)
    monkeypatch.setattr(proxy, 'IMAGE_CACHE_DIR', tmp_path / 'images')
    for module in (receipt_store, media_jobs):
        db = tmp_path / (module.__name__ + '.db')
        module.init_db(db)
        monkeypatch.setattr(module, 'DB_PATH', db)
        connect = module._conn
        monkeypatch.setattr(module, '_conn', lambda _path, fn=connect, path=db: fn(path))
    monkeypatch.setattr(proxy.uuid, 'uuid4', lambda: UUID(int=1))
    monkeypatch.setattr(receipts, 'uuid4', lambda: UUID(int=1))
    monkeypatch.setattr(media_ingestion.media_delivery, 'attach_provenance', lambda content, *a, **kw: content)
    monkeypatch.setattr(proxy.time, 'time', lambda: 1800000000.0)
    monkeypatch.setattr(proxy.time, 'monotonic_ns', lambda: 1800000000000000000)
    monkeypatch.setattr(proxy.ollama_protocol, '_iso_utc_now', lambda: '2027-01-15T08:00:00Z')
    monkeypatch.setattr(proxy, 'resolve_stable_alias', lambda *a, **kw: None)
    monkeypatch.setattr(proxy, '_resolve_request_target', lambda *a, **kw: None)
    monkeypatch.setattr(proxy, '_prepare_litellm_local_llm_request', AsyncMock(side_effect=lambda *a, **kw: (None, kw['body_json'])))
    monkeypatch.setattr(proxy, '_activate_prepared_litellm_local_target', AsyncMock(return_value=None))
    monkeypatch.setattr(proxy, '_litellm_runtime_config', lambda: SimpleNamespace(enabled=True, base_url='http://engine.test', api_key='test', service_id='test-service'))
    monkeypatch.setattr(proxy, '_profile_for_auth_context', lambda _: AppProfile(id='test'))
    monkeypatch.setattr(proxy, '_azizam_policy_decision', lambda *a, **kw: None)
    monkeypatch.setattr(proxy.ADMIN_STORE, 'get_service_registry', lambda: {'services': {}})
    monkeypatch.setattr(proxy, '_ensure_speech_service_ready', AsyncMock(return_value=None))
    monkeypatch.setattr(proxy, 'host_for_capability', lambda _: 'http://speech.test')
    monkeypatch.setattr(proxy, 'resolve_model_for_operation', lambda op, model: proxy.ModelResolution(model, model, op))
    monkeypatch.setattr(proxy, 'configured_downstream_headers', lambda *a, **kw: {})
    calls = []

    async def backend(request):
        calls.append(str(request.url))
        if request.url.path.endswith('/audio/speech'):
            return httpx.Response(200, content=__import__('test_sync_media_routes')._wav_bytes(), headers={'content-type': 'audio/wav'})
        body = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'object': 'list', 'model': 'test', 'data': [{'object': 'embedding', 'index': 0, 'embedding': [0.1, 0.2]}], 'usage': {'prompt_tokens': 1, 'total_tokens': 1}})
        if body.get('stream'):
            return httpx.Response(200, content=sse([chunk({'role': 'assistant', 'content': 'Hello'}), chunk({}, 'stop')]), headers={'content-type': 'text/event-stream', 'x-backend-test': 'yes'})
        return httpx.Response(200, json=chat_completion({'role': 'assistant', 'content': 'Hello'}), headers={'x-backend-test': 'yes'})

    real_client = httpx.AsyncClient
    class RoutingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.port == 19999:
                return await httpx.ASGITransport(app=proxy.app).handle_async_request(request)
            return await backend(request)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: real_client(*a, **{**kw, 'transport': RoutingTransport()}))

    async def image(**kw):
        calls.append('image-executor')
        return Response(content=b'{"created":1,"data":[{"b64_json":"aW1hZ2U="}]}', media_type='application/json', headers={'x-image-test': 'yes'})
    monkeypatch.setattr(proxy, '_generate_images_with_adaptive_batching', image)
    async def decision(*a, **kw):
        calls.append('decision-executor')
        return Response(content=b'{"decision":"yes"}', media_type='application/json', headers={'x-decision-test': 'yes'})
    monkeypatch.setattr(proxy.decision_provider_execution, 'openai', decision)
    async def systemone(*a, **kw):
        calls.append('systemone-executor')
        return proxy.decisions_api.DecisionDispatchResult(body={'model': 'test', 'usage': {}, 'answers': {'q': {'type': 'choice', 'choice': 'a', 'confidence': 1.0, 'probabilities': {'a': 1.0, 'b': 0.0}}}})
    monkeypatch.setattr(proxy.DECISION_RUNTIME, 'dispatch', systemone)
    return TestClient(proxy.app), calls


def snapshot(response):
    return {'status': response.status_code, 'headers': [list(item) for item in response.headers.multi_items()], 'body_b64': base64.b64encode(response.content).decode()}


@pytest.mark.parametrize('name,path,body', CASES, ids=[c[0] for c in CASES])
def test_default_response_differential(dispatch_env, name, path, body):
    client, calls = dispatch_env
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert calls, 'the comparison must reach a backend executor'
    fixture = Path(__file__).with_name('fixtures') / 'dispatch_admission_baseline.json'
    actual = snapshot(response)
    assert actual == json.loads(fixture.read_text())['responses'][name]


@pytest.mark.parametrize('name,path,body', CASES, ids=[c[0] for c in CASES])
def test_configured_resource_class_blocks_dispatch(dispatch_env, monkeypatch, name, path, body):
    client, calls = dispatch_env
    monkeypatch.setattr(proxy, 'MEDIA_JOB_ADMISSION_CFG', {'disabled_resource_classes': ['chat', 'embeddings', 'decision', 'image', 'audio']})
    response = client.post(path, json=body)
    assert response.status_code == 503, response.text
    assert not calls
    payload = response.json()
    if path.startswith('/api/'):
        assert isinstance(payload['error'], str)
        decision = payload['decision']
    else:
        assert payload['error']['code'] == 'resource_admission_denied'
        decision = payload['error']['decision']
        if path == '/v1/messages':
            assert payload['type'] == 'error'
            assert payload['error']['type'] == 'overloaded_error'
    assert 'resource_class_disabled' in payload['admission']['reason_codes']
    assert decision['reasons']


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'ollama'])
@pytest.mark.parametrize('cfg,override,code', [
    ({'disabled_intents': ['chat.generate']}, {}, 'intent_disabled'),
    ({'max_queue_depth': 1}, {}, 'queue_load_unknown'),
    ({'max_app_concurrency': {'mine': 1}}, {}, 'app_load_unknown'),
    ({}, {'state': 'maintenance', 'retry_after_seconds': 7, 'reason': 'private maintenance'}, 'service_unavailable'),
])
def test_limit_facts_and_protocol_projection(monkeypatch, cfg, override, code, protocol):
    monkeypatch.setattr(proxy, '_effective_media_job_admission_cfg', lambda: cfg)
    monkeypatch.setattr(proxy, '_operator_service_availability_overrides', lambda: {'selected': override})
    receipt = proxy.RouteReceipt(protocol='test', intent='chat.generate', app_id='mine')
    response = proxy._dispatch_admission_response(
        'chat.generate', resource_class='chat', service_id='selected',
        service_available=True, protocol=protocol, receipt=receipt,
    )
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert code in payload['admission']['reason_codes']
    assert b'private maintenance' not in response.body
    assert b'"mine"' not in response.body
    if protocol == 'ollama':
        assert isinstance(payload['error'], str)
        assert payload['decision']['reasons']
    else:
        assert payload['error']['decision']['reasons']
        assert payload['error']['type'] == ('overloaded_error' if protocol == 'anthropic' else 'service_unavailable')
    facts = receipt.resource_decision['facts']
    for fact in facts:
        if fact['name'] in {'queue_active', 'app_active'}:
            assert fact['value'] is None
            assert fact['quality'] == 'unavailable'
    if override:
        assert response.headers['retry-after'] == '7'


@pytest.mark.parametrize('kind,intent', [('image', 'image.generate'), ('audio', 'music.generate'), ('video', 'video.generate'), ('identity', 'image.edit')])
def test_worker_rechecks_before_executor(monkeypatch, kind, intent):
    monkeypatch.setattr(proxy, '_effective_media_job_admission_cfg', lambda: {'disabled_intents': [intent]})
    monkeypatch.setattr(proxy, '_operator_service_availability_overrides', lambda: {})
    monkeypatch.setattr(proxy, 'COMFY_ENABLED', True)
    monkeypatch.setattr(media_jobs, 'get', lambda *a, **kw: {'status': 'queued', 'intent': intent})
    failures = []
    executor = AsyncMock(side_effect=AssertionError('worker reached backend'))
    if kind == 'identity':
        monkeypatch.setattr(proxy, '_identity_job_failed', lambda *a: failures.append(a))
        monkeypatch.setattr(proxy.app.state, 'identity_execute', executor)
        asyncio.run(proxy._run_identity_job('job', None, proxy.NO_AUTH_CONTEXT, None, 'url'))
    else:
        monkeypatch.setattr(proxy, f'_load_{kind}_media_job_payload', lambda _: {})
        monkeypatch.setattr(proxy, f'_request_shim_from_{kind}_job_payload', lambda _: object())
        monkeypatch.setattr(proxy, '_auth_context_from_image_job_payload', lambda _: proxy.NO_AUTH_CONTEXT)
        monkeypatch.setattr(proxy, f'_mark_{kind}_media_job_failed', lambda *a, **kw: failures.append(a))
        target = {'image': 'proxy_comfy_request', 'audio': 'proxy_audio_generate', 'video': 'proxy_comfy_video_create'}[kind]
        monkeypatch.setattr(proxy, target, executor)
        asyncio.run(getattr(proxy, f'_run_{kind}_media_job')('job'))
    assert failures
    executor.assert_not_called()

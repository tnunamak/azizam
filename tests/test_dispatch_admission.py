"""Dispatch admission and exact default-response differential against 803f8350.

Only backend transports are fake: FK scenarios dispatch-admission-engine and
dispatch-admission-speech, plus executor spies for image and decisions. Public routes, protocol translations, routing,
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
from tests.support.access import authenticated, identity, plan_for  # noqa: E402
from gateway import media_jobs, receipt_store, receipts, media_ingestion
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture

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


class _DispatchCalls(list):
    """Executor spy entries plus every request the fake engines received."""

    def __init__(self, *fakes):
        super().__init__()
        self._fakes = fakes

    def __bool__(self):
        return bool(len(self)) or any(fake.requests for fake in self._fakes)

    def __repr__(self):
        return repr([*self, *(f'{r.method} {r.path}' for fake in self._fakes for r in fake.requests)])


@pytest.fixture
def dispatch_env(fake_upstreams, monkeypatch, tmp_path):
    signed_in = authenticated(identity(plan_for('test')))
    signed_in.start()
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
    engine = fake_upstreams.asgi(load_scenario('openai-compatible/dispatch-admission-engine'), host='engine.test')
    speech = fake_upstreams.asgi(load_scenario('openai-compatible/dispatch-admission-speech'), host='speech.test')
    fake_upstreams.router.add('127.0.0.1:19999', proxy.app)
    monkeypatch.setattr(proxy, '_litellm_runtime_config', lambda: SimpleNamespace(enabled=True, base_url=engine.base_url, api_key='test', service_id='test-service'))
    monkeypatch.setattr(proxy, '_azizam_policy_decision', lambda *a, **kw: None)
    monkeypatch.setattr(proxy.ADMIN_STORE, 'get_service_registry', lambda: {'services': {}})
    monkeypatch.setattr(proxy, '_ensure_speech_service_ready', AsyncMock(return_value=None))
    monkeypatch.setattr(proxy, 'host_for_capability', lambda _: speech.base_url)
    monkeypatch.setattr(proxy, 'resolve_model_for_operation', lambda op, model: proxy.ModelResolution(model, model, op))
    monkeypatch.setattr(proxy, 'configured_downstream_headers', lambda *a, **kw: {})
    calls = _DispatchCalls(engine, speech)

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
    monkeypatch.setattr(proxy.DECISION_RUNTIME, 'resolve', lambda _: SimpleNamespace(service='test-service'))
    yield TestClient(proxy.app), calls
    signed_in.stop()


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
        target = {'image': 'proxy_graph_image_request', 'audio': 'proxy_audio_generate', 'video': 'proxy_comfy_video_create'}[kind]
        monkeypatch.setattr(proxy, target, executor)
        asyncio.run(getattr(proxy, f'_run_{kind}_media_job')('job'))
    assert failures
    executor.assert_not_called()


@pytest.mark.parametrize('protocol', ['anthropic', 'ollama'])
@pytest.mark.parametrize('stream', [False, True])
def test_late_dispatch_refusal_keeps_protocol_decision(dispatch_env, monkeypatch, protocol, stream):
    client, calls = dispatch_env
    original = proxy._dispatch_admission_response
    count = 0
    def late(intent, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return None
        return original(intent, **kwargs)
    # Initial frontend gate allows; resolved inner dispatch observes a changed rule.
    monkeypatch.setattr(proxy, '_dispatch_admission_response', late)
    monkeypatch.setattr(proxy, 'MEDIA_JOB_ADMISSION_CFG', {'disabled_intents': ['chat.generate']})
    path = '/v1/messages' if protocol == 'anthropic' else '/api/chat'
    response = client.post(path, json={'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 10, 'stream': stream})
    assert response.status_code == 503
    payload = response.json()
    if protocol == 'anthropic':
        assert payload['type'] == 'error'
        assert payload['error']['decision']['reasons']
    else:
        assert isinstance(payload['error'], str)
        assert payload['decision']['reasons']
    assert not calls


def test_worker_does_not_count_its_own_queue_and_restores_context(monkeypatch):
    monkeypatch.setattr(proxy, '_effective_media_job_admission_cfg', lambda: {'max_queue_depth': 1})
    monkeypatch.setattr(proxy, '_operator_service_availability_overrides', lambda: {})
    monkeypatch.setattr(proxy, '_media_job_queue_depth', lambda *a, **kw: 1)
    args = dict(service_id='test-media', service_available=True, resource_class='image', queue_key=proxy._MEDIA_JOB_QUEUE_KEY)
    assert proxy._dispatch_admission_response('image.generate', **args).status_code == 503
    token = proxy._ADMISSION_WORKER.set(True)
    try:
        assert proxy._dispatch_admission_response('image.generate', **args) is None
    finally:
        proxy._ADMISSION_WORKER.reset(token)
    assert not proxy._ADMISSION_WORKER.get()


def test_unknown_service_is_not_a_known_available_service(monkeypatch):
    monkeypatch.setattr(proxy, '_effective_media_job_admission_cfg', lambda: {'disabled_intents': ['unrelated']})
    monkeypatch.setattr(proxy, '_operator_service_availability_overrides', lambda: {})
    response = proxy._dispatch_admission_response('chat.generate', resource_class='chat')
    assert 'service_unknown' in json.loads(response.body)['admission']['reason_codes']
    assert proxy._dispatch_admission_response('chat.generate', resource_class='chat', service_available=True) is None


def test_app_concurrency_owner_input_validation():
    from gateway.admin_api import _validate_media_job_admission_overrides, _RouteError
    assert _validate_media_job_admission_overrides({'max_app_concurrency': {'mine': 2}})['max_app_concurrency'] == {'mine': 2}
    for invalid in ([], {'mine': None}, {'mine': -1}, {'': 1}):
        with pytest.raises(_RouteError):
            _validate_media_job_admission_overrides({'max_app_concurrency': invalid})


@pytest.mark.parametrize('protocol', ['anthropic', 'ollama'])
@pytest.mark.parametrize('stream', [False, True])
def test_non_admission_upstream_error_keeps_legacy_envelope(dispatch_env, monkeypatch, protocol, stream):
    client, _ = dispatch_env
    class ErrorStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":{"message":"ordinary backend error"}}'
    async def forward(*args, **kwargs):
        response = httpx.Response(502, stream=ErrorStream())
        if stream:
            return SimpleNamespace(aclose=AsyncMock()), response
        await response.aread()
        return response
    monkeypatch.setattr(proxy, '_forward_chat_completions', forward)
    path = '/v1/messages' if protocol == 'anthropic' else '/api/chat'
    response = client.post(path, json={'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 10, 'stream': stream})
    assert response.status_code == 502
    payload = response.json()
    if protocol == 'anthropic':
        assert payload['error']['message'] == 'ordinary backend error'
        assert payload['type'] == 'error'
    else:
        assert payload == {'error': 'ordinary backend error'}
    assert 'admission' not in payload


@pytest.mark.parametrize('path,body,intent', [
    ('/v1/messages', {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 1}, 'chat.messages'),
    ('/api/chat', {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': False}, 'chat.completions'),
    ('/api/generate', {'model': 'test', 'prompt': 'hi', 'stream': False}, 'chat.generate'),
    ('/v1/responses', {'model': 'test', 'input': 'hi'}, 'chat.responses'),
])
def test_facade_uses_its_declared_intent(dispatch_env, monkeypatch, path, body, intent):
    client, calls = dispatch_env
    monkeypatch.setattr(proxy, 'MEDIA_JOB_ADMISSION_CFG', {'disabled_intents': [intent]})
    response = client.post(path, json=body)
    assert response.status_code == 503
    assert 'intent_disabled' in response.json()['admission']['reason_codes']
    assert not calls


def test_unconfigured_dispatch_still_evaluates_without_inventing_availability(monkeypatch):
    monkeypatch.setattr(proxy, '_effective_media_job_admission_cfg', lambda: {})
    monkeypatch.setattr(proxy, '_operator_service_availability_overrides', lambda: {})
    original = proxy.resource_admission_http.advise_route_admission
    seen = []
    def advise(**kwargs):
        decision = original(**kwargs)
        seen.append(decision)
        return decision
    monkeypatch.setattr(proxy.resource_admission_http, 'advise_route_admission', advise)
    receipt = proxy.RouteReceipt(protocol='test', intent='chat.generate')
    assert proxy._dispatch_admission_response('chat.generate', resource_class='chat', receipt=receipt) is None
    assert len(seen) == 1 and seen[0].allowed
    available = next(fact for fact in seen[0].details.facts if fact.name == 'available')
    assert available.value is None and available.quality == 'unavailable'
    assert receipt.resource_decision is None


def test_image_rechecks_policy_after_async_preparation(dispatch_env, monkeypatch):
    client, calls = dispatch_env
    original = proxy._apply_workflow_extensions_to_image_request
    async def prepare(*args, **kwargs):
        result = await original(*args, **kwargs)
        monkeypatch.setattr(proxy, 'MEDIA_JOB_ADMISSION_CFG', {'disabled_intents': ['image.generate']})
        return result
    monkeypatch.setattr(proxy, '_apply_workflow_extensions_to_image_request', prepare)
    response = client.post('/v1/images/generations', json={'model': 'flux2-klein-4b', 'prompt': 'hi'})
    assert response.status_code == 503
    assert 'intent_disabled' in response.json()['admission']['reason_codes']
    assert not calls


def test_speech_rechecks_policy_after_service_start(dispatch_env, monkeypatch):
    client, calls = dispatch_env
    async def start(*args, **kwargs):
        monkeypatch.setattr(proxy, 'MEDIA_JOB_ADMISSION_CFG', {'disabled_intents': ['speech.synthesis']})
        return None
    monkeypatch.setattr(proxy, '_ensure_speech_service_ready', start)
    response = client.post('/v1/audio/speech', json={'model': 'tts-1', 'input': 'hi', 'voice': 'alloy'})
    assert response.status_code == 503
    assert not calls


def test_enabled_transport_does_not_claim_unknown_target_is_available(dispatch_env, monkeypatch):
    client, calls = dispatch_env
    monkeypatch.setattr(proxy, 'MEDIA_JOB_ADMISSION_CFG', {'disabled_intents': ['unrelated']})
    # The initial route check passed. Only the transport is configured; there is
    # no target availability fact at the final handoff.
    monkeypatch.setattr(proxy, '_model_dispatch_admission_response', lambda *a, **kw: None)
    monkeypatch.setattr(proxy, '_resolve_request_target', lambda *a, **kw: SimpleNamespace(logical_service_id='unknown-target'))
    response = client.post('/v1/chat/completions', json={'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert response.status_code == 503
    assert 'service_unknown' in response.json()['admission']['reason_codes']
    assert not calls


# The live owner's stored overrides: every key present, every value empty.
LIVE_SHAPED_OVERRIDES = {"disabled_intents": [], "disabled_resource_classes": [],
                         "max_queue_depth": None, "reserved_tokens": [], "retry_after_seconds": None}


@pytest.mark.parametrize('name,path,body', CASES, ids=[c[0] for c in CASES])
def test_empty_stored_overrides_keep_default_responses(dispatch_env, monkeypatch, name, path, body):
    client, calls = dispatch_env
    monkeypatch.setattr(proxy, '_operator_media_job_admission_overrides', lambda: dict(LIVE_SHAPED_OVERRIDES))
    monkeypatch.setattr(proxy, '_operator_service_availability_overrides', lambda: {})
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert calls, 'the comparison must reach a backend executor'
    fixture = Path(__file__).with_name('fixtures') / 'dispatch_admission_baseline.json'
    assert snapshot(response) == json.loads(fixture.read_text())['responses'][name]

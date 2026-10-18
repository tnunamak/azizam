"""Real-test evidence belongs to the exact saved connection and model, not client claims."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from gateway import engine_admin, service_registry

OWNER = SimpleNamespace(roles=('owner',))


class Store:
    def __init__(self, path, **config):
        self.path = path
        self.saved = service_registry.service_from_backend_config('review', {
            'host': '127.0.0.1', 'port': 5051, 'base_path': '/v1',
            'protocol': 'openai-compatible', 'definition': 'openai-compatible',
            'capabilities': ['chat'],
            'models': [{'model_group': 'review-chat', 'upstream_model': 'review'}],
            **config,
        }, source='ui')
        self.written = None

    def get_service_registry(self):
        return {'services': {'review': copy.deepcopy(self.saved)}}

    def list_access_plans(self):
        return []

    def write_engine(self, **kwargs):
        self.written = kwargs['raw']
        return kwargs['raw']


@pytest.fixture
def service(tmp_path, monkeypatch):
    async def real_test(*args, **kwargs):
        return {'ok': True, 'at': 123, 'seconds': .1, 'via': 'draft'}
    async def check(*args, **kwargs):
        return {'outcome': 'ok', 'checked_at': 123, 'models': []}
    monkeypatch.setattr(engine_admin, 'run_real_test', real_test)
    monkeypatch.setattr(engine_admin, 'run_check', check)
    return engine_admin.EngineService(Store(tmp_path / 'admin.db'))


def body(service, **changes):
    shown = service.view('review', OWNER)
    return {'service_id': 'review', 'definition': shown['definition'],
            'values': {**shown['values'], **changes}, 'capability': 'chat', 'model': 'review'}


def test_saved_result_binds_the_model_and_has_server_receipt(service):
    result = asyncio.run(service.test(OWNER, body(service)))
    assert result['via'] == 'saved'
    assert result['model'] == 'review'
    assert result['evidence_token']


@pytest.mark.parametrize('change', [
    {'address': 'http://127.0.0.1:5052/v1'}, {'path_prefix': '/draft'},
    {'reply_timeout_s': 57}, {'headers': [{'name': 'X-Tenant', 'value': 'draft'}]},
    {'transforms': [{'kind': 'default-params', 'set': {'temperature': .3}}]},
    {'api_key': 'test-only-draft-credential'},
])
def test_unsaved_settings_are_never_labeled_saved(service, change):
    result = asyncio.run(service.test(OWNER, body(service, **change)))
    assert result['via'] == 'draft'
    assert 'evidence_token' not in result
    assert 'test-only-draft-credential' not in str(result)


def test_unsaved_model_is_draft(service):
    draft = body(service)
    draft['model'] = 'unsaved-model'
    result = asyncio.run(service.test(OWNER, draft))
    assert result['via'] == 'draft'
    assert result['model'] == 'unsaved-model'


def test_client_cannot_invent_saved_evidence(service):
    asyncio.run(service.update(OWNER, 'review', {
        'disabled': True, 'values': body(service)['values'],
        'tests': {'chat': {'ok': True, 'at': 123, 'via': 'saved', 'evidence_token': 'invented'}},
    }, expected_generation=0))
    assert not service.store.written.get('engine_tests')


def test_only_server_result_is_persisted_from_receipt(service):
    result = asyncio.run(service.test(OWNER, body(service)))
    result.update(ok=False, at=999, model='forged-model')
    asyncio.run(service.update(OWNER, 'review', {
        'disabled': True, 'values': body(service)['values'], 'tests': {'chat': result},
    }, expected_generation=0))
    saved = service.store.written['engine_tests']['chat']
    assert saved['ok'] is True and saved['at'] == 123
    assert saved['model'] == 'review'


@pytest.mark.parametrize('change', [
    {'path_prefix': '/draft'}, {'reply_timeout_s': 57},
    {'headers': [{'name': 'X-Tenant', 'value': 'draft'}]},
    {'transforms': [{'kind': 'default-params', 'set': {'temperature': .3}}]},
    {'api_key': 'test-only-new-credential'},
])
def test_saved_receipt_cannot_survive_changed_settings(service, change):
    result = asyncio.run(service.test(OWNER, body(service)))
    service.store.saved['config']['engine_tests'] = {'chat': copy.deepcopy(result)}
    asyncio.run(service.update(OWNER, 'review', {
        'disabled': True, 'values': body(service, **change)['values'], 'tests': {'chat': result},
    }, expected_generation=0))
    assert not service.store.written.get('engine_tests')


def test_model_config_change_invalidates_saved_evidence(service):
    result = asyncio.run(service.test(OWNER, body(service)))
    service.store.saved['config']['engine_tests'] = {'chat': result}
    asyncio.run(service.update(OWNER, 'review', {
        'disabled': True, 'values': body(service)['values'],
        'models': [{'id': 'review', 'public_name': 'review-chat', 'context_window': 2048}],
        'tests': {'chat': result},
    }, expected_generation=0))
    assert not service.store.written.get('engine_tests')


@pytest.mark.parametrize('operation', ['check', 'test'])
@pytest.mark.parametrize('problem', ['missing', 'protocol', 'definition'])
def test_probes_reject_unknown_or_incompatible_saved_contract(service, operation, problem):
    request = body(service)
    if problem == 'missing':
        request['service_id'] = 'absent'
    elif problem == 'protocol':
        service.store.saved['config']['protocol'] = 'unsupported-test-protocol'
    else:
        request['definition'] = 'own-model-list'
    with pytest.raises((engine_admin.DraftError, engine_admin.EngineRouteError)):
        asyncio.run(getattr(service, operation)(OWNER, request))

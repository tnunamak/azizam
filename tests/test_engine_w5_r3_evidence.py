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


def test_every_engine_view_url_uses_shared_redaction(service):
    service.store.saved['config'].update(
        address='http://user:test-password@127.0.0.1:5051/v1?token=test-query',
        base_url='http://user:test-password@127.0.0.1:5051/v1?token=test-query')
    shown = service.view('review', OWNER)
    for value in (shown['values']['address'], shown['record']['config']['address'],
                  shown['v2']['connection']['address'], shown['request_path']['url'],
                  *(row['url'] for row in shown['request_path']['operations'])):
        assert 'test-password' not in value and 'test-query' not in value


def test_hidden_headers_restore_once_and_keep_the_name_binding(service):
    hidden = [{'name': 'X-Token', 'value': 'test-hidden-credential', 'secret': True}]
    service.store.saved['config']['headers'] = hidden
    values = body(service)['values']
    asyncio.run(service.update(OWNER, 'review', {'disabled': True, 'values': values}, expected_generation=0))
    assert service.store.written['headers'] == hidden


def test_admin_connection_edit_is_refused_before_network(service, monkeypatch):
    calls = []
    async def check(*args, **kwargs):
        calls.append('outbound')
        return {'outcome': 'ok', 'checked_at': 123}
    monkeypatch.setattr(engine_admin, 'run_check', check)
    with pytest.raises((engine_admin.EngineRouteError, PermissionError)):
        asyncio.run(service.update(SimpleNamespace(roles=('admin',)), 'review', {
            'values': {'address': 'http://127.0.0.1:5052/v1'},
        }, expected_generation=0))
    assert calls == []


def test_ready_timeout_can_change_without_editing_list_start(service):
    lifecycle = {'driver': 'command', 'managed': True,
                 'driver_config': {'start': ['runner', '--port', '5051'], 'health_timeout_s': 120}}
    service.store.saved['config']['lifecycle'] = lifecycle
    service.store.saved['lifecycle'] = lifecycle
    values = body(service, lifecycle_ready_s=180)['values']
    asyncio.run(service.update(OWNER, 'review', {'disabled': True, 'values': values}, expected_generation=0))
    assert service.store.written['lifecycle']['driver_config']['start'] == ['runner', '--port', '5051']
    assert service.store.written['lifecycle']['driver_config']['health_timeout_s'] == 180


def test_view_explains_why_saved_key_will_be_withheld(service):
    service.store.saved['config'].update(api_key_env='W5_TEST_ENV_KEY', credential_origin='http://elsewhere.test:80')
    service.env = lambda name: 'test-only-env-key'
    shown = service.view('review', OWNER)
    assert any(row['code'] == 'engine_credential_origin_mismatch' for row in shown['attention'])
    assert 'test-only-env-key' not in str(shown)


def test_real_test_trace_does_not_return_hidden_header_values(service, monkeypatch):
    hidden = 'test-hidden-trace-credential'
    service.store.saved['config']['headers'] = [{'name': 'X-Token', 'value': hidden, 'secret': True}]
    async def real_test(*args, **kwargs):
        return {'ok': True, 'at': 123, 'seconds': .1,
                'request': {'url': 'http://127.0.0.1:5051/v1/chat/completions',
                            'headers': {'X-Token': hidden, 'X-Tenant': 'public'}}}
    monkeypatch.setattr(engine_admin, 'run_real_test', real_test)
    result = asyncio.run(service.test(OWNER, body(service)))
    assert hidden not in str(result)
    assert result['request']['headers']['X-Tenant'] == 'public'


def test_saved_receipt_cannot_be_used_for_another_capability(service):
    result = asyncio.run(service.test(OWNER, body(service)))
    asyncio.run(service.update(OWNER, 'review', {
        'disabled': True, 'values': body(service)['values'], 'tests': {'embeddings': result},
    }, expected_generation=0))
    assert not service.store.written.get('engine_tests')


@pytest.mark.parametrize('operation', ['check', 'test'])
def test_admin_probe_cannot_send_edited_saved_connection(service, monkeypatch, operation):
    calls = []
    async def outbound(*args, **kwargs):
        calls.append('outbound')
        return {'ok': True, 'at': 123, 'outcome': 'ok'}
    monkeypatch.setattr(engine_admin, 'run_check' if operation == 'check' else 'run_real_test', outbound)
    with pytest.raises((engine_admin.EngineRouteError, PermissionError)):
        asyncio.run(getattr(service, operation)(SimpleNamespace(roles=('admin',)),
                                              body(service, address='http://127.0.0.1:5052/v1')))
    assert calls == []


def test_unbound_legacy_test_is_not_saved_settings_proof(service):
    service.store.saved['config']['engine_tests'] = {'chat': {'ok': True, 'at': 123, 'via': 'saved'}}
    assert service.view('review', OWNER)['tests'] == {}
    asyncio.run(service.update(OWNER, 'review', {
        'disabled': True, 'values': body(service)['values'],
    }, expected_generation=0))
    assert not service.store.written.get('engine_tests')


def test_command_arrays_are_editable_and_save_exact_arguments(service):
    lifecycle = {'driver': 'command', 'managed': True,
                 'driver_config': {'start': ['runner', '--port', '5051'], 'health_timeout_s': 120}}
    service.store.saved['config']['lifecycle'] = lifecycle
    service.store.saved['lifecycle'] = lifecycle
    assert service.view('review', OWNER)['lifecycle_editable'] is True
    argv = ['runner', 'with spaces', 'comma,value', '"quoted"', '']
    asyncio.run(service.update(OWNER, 'review', {
        'disabled': True, 'values': body(service, lifecycle_start=argv)['values'],
    }, expected_generation=0))
    assert service.store.written['lifecycle']['driver_config']['start'] == argv


def test_saved_evidence_is_hidden_after_another_registry_path_changes_connection(service):
    result = asyncio.run(service.test(OWNER, body(service)))
    asyncio.run(service.update(OWNER, 'review', {
        'disabled': True, 'values': body(service)['values'], 'tests': {'chat': result},
    }, expected_generation=0))
    service.store.saved['config']['engine_tests'] = service.store.written['engine_tests']
    assert service.view('review', OWNER)['tests']['chat']['ok'] is True
    service.store.saved['config']['headers'] = [{'name': 'X-Tenant', 'value': 'changed-elsewhere'}]
    assert service.view('review', OWNER)['tests'] == {}


@pytest.mark.parametrize('change', [
    {'enabled': True}, {'context_window': 8192}, {'public_name': 'draft-public-name'},
])
def test_draft_model_settings_are_not_saved_settings_proof(service, change):
    if 'enabled' in change:
        service.store.saved['config']['models'][0]['enabled'] = False
    request = body(service)
    request['models'] = [{**service.view('review', OWNER)['models'][0], **change}]
    result = asyncio.run(service.test(OWNER, request))
    assert result['via'] == 'draft'
    assert 'evidence_token' not in result


def test_unchanged_submitted_model_settings_can_prove_saved_settings(service):
    request = body(service)
    request['models'] = service.view('review', OWNER)['models']
    result = asyncio.run(service.test(OWNER, request))
    assert result['via'] == 'saved'
    assert result['evidence_token']

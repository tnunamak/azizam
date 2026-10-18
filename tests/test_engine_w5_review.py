"""Review regressions: a page read and unchanged save preserve engine behavior."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

import proxy
from gateway import engine_admin, service_registry
from gateway.engine_definitions import load_engine_catalog
from gateway.engine_connection import InferenceConnectionError, inference_connection
from gateway.lifecycle_drivers import ExternalDriver, engine_read_request
from gateway.litellm_substrate import compile_litellm_config_from_registry, router_reply_timeout
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401


def record(**config):
    return service_registry.service_from_backend_config('review', {
        'host': '127.0.0.1', 'port': 5051, 'base_path': '/v1', 'protocol': 'openai-compatible',
        'capabilities': ['chat'], 'gateway_lifecycle': True,
        'models': [{'model_group': 'review-chat', 'upstream_model': 'review'}], **config,
    }, source='ui')


def view(saved, tmp_path):
    return engine_admin.engine_view(load_engine_catalog(), saved, db_path=tmp_path / 'admin.db')


@pytest.mark.parametrize('saved', [None, 37, 900])
def test_current_alias_never_invents_a_reply_timeout(saved):
    services = {'review': record(**({'timeout': saved} if saved else {}))}
    compiled = compile_litellm_config_from_registry({'services': services})
    for alias in ('current', 'current-fast'):
        params = next(row['litellm_params'] for row in compiled['model_list'] if row['model_name'] == alias)
        assert params.get('timeout') == saved
        assert router_reply_timeout({'services': services}, alias) == saved
    services['unbounded'] = record()
    assert router_reply_timeout({'services': services}, 'current') is None


class Store:
    def __init__(self, saved, path):
        self.saved, self.path = saved, path
    def list_access_plans(self):
        return []
    def get_service_registry(self):
        return {'services': {'review': copy.deepcopy(self.saved)}}
    def write_engine(self, **kwargs):
        self.written = kwargs['raw']
        return kwargs['raw']


def test_echoed_connection_timeout_does_not_create_a_stored_limit(tmp_path):
    saved = record()
    store = Store(saved, tmp_path / 'admin.db')
    service = engine_admin.EngineService(store, hop_timeout=lambda: 1800.0)
    before = service.view('review', SimpleNamespace(roles=('owner',)))
    asyncio.run(service.update(SimpleNamespace(roles=('owner',)), 'review', {
        'definition': before['definition'], 'disabled': True,
        'values': {**before['values'], 'reply_timeout_s': 1800, 'headers': [{'name': 'X-Tenant', 'value': 'household'}]},
    }, expected_generation=0))
    assert 'timeout' not in store.written
    assert store.written['headers'] == [{'name': 'X-Tenant', 'value': 'household'}]


def test_effective_timeout_names_the_runtime_default(tmp_path):
    shown = engine_admin.engine_view(load_engine_catalog(), record(), db_path=tmp_path / 'admin.db', hop_timeout=1800)
    assert shown['values']['reply_timeout_s'] == 1800
    assert shown['reply_timeout'] == {'seconds': 1800, 'source': 'default for engines Azizam starts'}


def test_missing_env_key_blocks_inference_and_is_an_attention_item(tmp_path, monkeypatch):
    monkeypatch.delenv('W5_UNSET_TEST_KEY', raising=False)
    saved = record(api_key_env='W5_UNSET_TEST_KEY', credential_origin='http://127.0.0.1:5051')
    with pytest.raises(InferenceConnectionError):
        inference_connection(saved, '/chat/completions', fallback_address='', fallback_timeout=1800,
                             db_path=tmp_path / 'admin.db', env=lambda _: None)
    assert {'code': 'engine_credential_unavailable', 'message': 'Key not available: W5_UNSET_TEST_KEY is not set'} in view(saved, tmp_path)['attention']


def test_missing_key_still_allows_health_and_resource_checks(monkeypatch, fake_upstreams):
    monkeypatch.delenv('W5_UNSET_TEST_KEY', raising=False)
    fake = fake_upstreams.asgi(scenario_from_data({
        'protocol': 'openai-compatible', 'name': 'missing-key',
        'provenance': {'source': 'hand-written', 'reference': 'D19: probes proceed without an unavailable key'},
        'exchanges': [{'id': 'health', 'times': 2, 'match': {'method': 'GET', 'path': '/health',
                        'headers': {'authorization': {'$absent': True}}}, 'response': {'status': 200, 'json': {'ok': True}}}],
    }), host='review.fk.test')
    config = {'definition': 'openai-compatible', 'base_url': fake.base_url + '/v1',
              'api_key_env': 'W5_UNSET_TEST_KEY', 'credential_origin': 'http://review.fk.test:80'}
    assert asyncio.run(ExternalDriver().ensure_ready(config, timeout_s=0))
    assert asyncio.run(proxy._probe_service_resource_fact(config)) == {'status': 'available'}


@pytest.mark.parametrize('config,url', [
    ({'base_url': 'https://openrouter.ai/api/v1', 'base_path': '/api/v1', 'api_key_env': 'W5_PROBE_KEY',
      'credential_origin': 'https://openrouter.ai:443'}, 'https://openrouter.ai/api/v1/health'),
    ({'host': '127.0.0.1', 'port': 6061, 'base_path': '/openai', 'api_key_env': 'W5_PROBE_KEY',
      'credential_origin': 'http://127.0.0.1:6061'}, 'http://127.0.0.1:6061/health'),
])
def test_unedited_resource_probes_keep_the_main_url_and_no_key(config, url, monkeypatch):
    monkeypatch.setenv('W5_PROBE_KEY', 'test-only-value')
    sent = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, address, **kwargs):
            sent.append((address, kwargs.get('headers', {})))
            return SimpleNamespace(status_code=200)
    monkeypatch.setattr(proxy.httpx, 'AsyncClient', Client)
    assert asyncio.run(proxy._probe_service_resource_fact(config)) == {'status': 'available'}
    assert sent == [(url, {})]


def test_unedited_lifecycle_probe_keeps_the_root_path_and_no_key(monkeypatch):
    monkeypatch.setenv('W5_PROBE_KEY', 'test-only-value')
    assert engine_read_request({'host': '127.0.0.1', 'port': 6061, 'base_path': '/openai',
                                'api_key_env': 'W5_PROBE_KEY', 'credential_origin': 'http://127.0.0.1:6061'},
                               '/health') == ('http://127.0.0.1:6061/health', {})


def test_hidden_headers_survive_an_unchanged_save_and_can_be_removed(tmp_path):
    hidden = [{'name': 'X-Api-Key', 'value': 'test-only-header'},
              {'name': 'X-Tenant', 'value': 'private-test-value', 'secret': True}]
    saved = record(headers=hidden)
    store = Store(saved, tmp_path / 'admin.db')
    service = engine_admin.EngineService(store)
    shown = service.view('review', SimpleNamespace(roles=('owner',)))
    assert len(shown['values']['headers']) == 2
    assert all(row['not_sent'] and row['value'] == '[redacted]' for row in shown['values']['headers'])
    assert 'test-only-header' not in str(shown)
    for rows, expected in ((shown['values']['headers'], hidden), ([], [])):
        asyncio.run(service.update(SimpleNamespace(roles=('owner',)), 'review', {
            'definition': shown['definition'], 'disabled': True, 'values': {'headers': rows},
        }, expected_generation=0))
        assert store.written.get('headers', []) == expected


def test_env_mask_is_the_same_in_both_record_views(tmp_path):
    shown = view(record(lifecycle={'driver': 'command', 'driver_config': {'start': 'server', 'env': {'TOKEN': 'test-env'}}}), tmp_path)
    assert shown['record']['lifecycle']['driver_config']['env']['TOKEN'] == '[redacted]'
    assert shown['v2']['process']['env']['TOKEN'] == '[redacted]'


@pytest.mark.parametrize('start', ['runner --token test-private-value', ['runner', '--token', 'test-private-value']])
def test_echoed_command_markers_leave_the_stored_process_unchanged(tmp_path, start):
    lifecycle = {'driver': 'command', 'driver_config': {'start': start, 'env': {'MODE': 'private-env-value'}}}
    saved = record(lifecycle=lifecycle)
    store = Store(saved, tmp_path / 'admin.db')
    service = engine_admin.EngineService(store)
    shown = service.view('review', SimpleNamespace(roles=('owner',)))
    asyncio.run(service.update(SimpleNamespace(roles=('owner',)), 'review', {
        'definition': shown['definition'], 'disabled': True, 'values': shown['values'],
    }, expected_generation=0))
    assert store.written['lifecycle'] == service_registry.legacy_lifecycle_from_service(saved)


def test_hidden_header_markers_are_bound_to_the_stored_header_name(tmp_path):
    saved = record(headers=[{'name': 'X-Token', 'value': 'private-a'},
                            {'name': 'X-Api-Key', 'value': 'private-b'}])
    store = Store(saved, tmp_path / 'admin.db')
    service = engine_admin.EngineService(store)
    shown = service.view('review', SimpleNamespace(roles=('owner',)))
    rows = list(reversed(shown['values']['headers']))
    asyncio.run(service.update(SimpleNamespace(roles=('owner',)), 'review', {
        'definition': shown['definition'], 'disabled': True, 'values': {'headers': rows},
    }, expected_generation=0))
    assert store.written['headers'] == list(reversed(saved['config']['headers']))
    rows[0]['name'] = 'X-Changed-Token'
    with pytest.raises(engine_admin.DraftError):
        asyncio.run(service.update(SimpleNamespace(roles=('owner',)), 'review', {
            'definition': shown['definition'], 'disabled': True, 'values': {'headers': rows},
        }, expected_generation=0))

"""Round-three regressions for metadata authorization, probe paths and D20."""
from __future__ import annotations

import copy
import json

import pytest

from gateway import admin_store, service_registry
from gateway.lifecycle_drivers import engine_read_request
from tests.support.fake_upstream import fake_upstreams  # noqa: F401
from tests.test_engine_admin_routes import (  # noqa: F401
    _edit, _put_registry, _record, _registry, fake_engine_dns, fixed_baseline,
)
from tests.test_signin_who_can_enter import gateway, owner_and_admin  # noqa: F401


@pytest.mark.parametrize('change', [{'disabled': True}, {'note': 'maintenance note'}])
def test_admin_metadata_patch_preserves_untouched_hop_config(gateway, fake_upstreams, change):
    _, _, _, admin = owner_and_admin(gateway)
    registry = _registry(gateway)
    saved = service_registry.service_from_backend_config('hop', {
        'name': 'Hop', 'host': 'hop.fk.test', 'port': 80, 'base_path': '/v1',
        'protocol': 'openai-compatible', 'gateway_lifecycle': True,
        'capabilities': ['chat', 'openai'],
        'models': [{'id': 'offering', 'model_group': 'hop-chat', 'upstream_model': 'up-model'}],
    }, source='ui')
    registry['services']['hop'] = saved
    _put_registry(gateway, registry)
    response = _edit(admin, 'hop', {**change, 'acknowledge_warnings': True})
    assert response.status_code == 200, response.text
    before = copy.deepcopy(saved['config'])
    after = dict(_record(gateway, 'hop')['config'])
    for key in ('disabled', 'enabled', 'note'):
        before.pop(key, None)
        after.pop(key, None)
    assert after == before


@pytest.mark.parametrize('config, expected', [
    ({'base_url': 'https://router.fk.test/api/v1'}, 'https://router.fk.test/api/v1/health'),
    ({'base_url': 'https://engine.fk.test/custom'}, 'https://engine.fk.test/custom/health'),
    ({'host': '127.0.0.1', 'port': 6061, 'base_path': '/openai'}, 'http://127.0.0.1:6061/health'),
    ({'host': '127.0.0.1', 'port': 6062, 'base_path': '/api'}, 'http://127.0.0.1:6062/health'),
    ({'host': '127.0.0.1', 'port': 6063, 'base_path': '/v1'}, 'http://127.0.0.1:6063/health'),
])
def test_saved_connection_keeps_health_endpoint_semantics(config, expected):
    assert engine_read_request({**config, 'probe_with_inference_connection': True}, '/health') == (expected, {})


def test_timeout_600_migrates_once_only_for_lifecycle_hop_records(tmp_path):
    path = tmp_path / 'admin.db'
    admin_store.init_db(path)
    registry = {'services': {}}
    for name, fields in {
        'old-default': {'timeout': 600, 'gateway_lifecycle': True},
        'chosen': {'timeout': 120, 'gateway_lifecycle': True},
        'missing': {'gateway_lifecycle': True},
        'string': {'timeout': '600', 'gateway_lifecycle': True},
        'direct': {'timeout': 600},
        'deployment-hop': {'timeout': 600, 'models': [{'id': 'chat', 'upstream_model': 'up', 'gateway_lifecycle': True}]},
    }.items():
        registry['services'][name] = service_registry.service_from_backend_config(name, {
            'host': '127.0.0.1', 'port': 6061, 'protocol': 'openai-compatible',
            'capabilities': ['chat', 'openai'], 'models': [{'id': 'chat', 'upstream_model': 'up'}], **fields,
        }, source='ui')
    with admin_store._conn(path) as conn:
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES ('service_registry',?,0)", (json.dumps(registry),))
        conn.execute('UPDATE schema_version SET version=10')
        conn.commit()
    admin_store.init_db(path)
    migrated = admin_store.get_service_registry_with_generation(path=path)['value']
    for name in ('old-default', 'deployment-hop', 'missing'):
        assert 'timeout' not in migrated['services'][name]['config']
    assert migrated['services']['chosen']['config']['timeout'] == 120
    assert migrated['services']['direct']['config']['timeout'] == 600
    assert migrated['services']['string']['config']['timeout'] == '600'
    # A deliberate post-upgrade 600 survives every later startup.
    migrated['services']['old-default']['config']['timeout'] = 600
    with admin_store._conn(path) as conn:
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(migrated),))
        conn.commit()
    admin_store.init_db(path)
    assert admin_store.get_service_registry_with_generation(path=path)['value'] == migrated

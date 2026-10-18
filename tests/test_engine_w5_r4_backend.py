"""The engine editor writes secret commands and reads one public registry revision."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from gateway import admin_store, engine_admin, secret_commands, secret_store, service_registry
from gateway.engine_definitions import load_engine_catalog


def record(**config):
    return service_registry.service_from_backend_config('editor', {
        'host': '127.0.0.1', 'port': 8080, 'base_path': '/v1',
        'protocol': 'openai-compatible', 'definition': 'openai-compatible',
        'capabilities': ['chat'], 'models': [{'model_group': 'editor-chat', 'upstream_model': 'test'}], **config,
    }, source='ui')


def apply(saved, command):
    writes = []
    def put(value, origin):
        writes.append((value, origin))
        return 'sref_' + str(len(writes)).zfill(24)
    updated, outcome = secret_commands.apply_commands(saved, secret_commands.parse_commands({'commands': [command]}), put)
    return updated, outcome, writes


def test_provider_key_add_replace_retain_clear_has_stable_id_and_origin(tmp_path):
    saved, _, writes = apply(record(), {'carrier': 'config.api_key', 'operation': 'add', 'value': 'new-key-for-editor'})
    assert writes == [('new-key-for-editor', 'http://127.0.0.1:8080')]
    shown = engine_admin.engine_view(load_engine_catalog(), saved, db_path=tmp_path / 'admin.db')
    entry_id = shown['key']['entry_id']
    assert entry_id.startswith('ent_') and shown['key']['state'] == 'configured'
    assert 'new-key-for-editor' not in str(shown)
    kept, outcome, writes = apply(saved, {'carrier': 'config.api_key', 'operation': 'retain', 'entry_id': entry_id})
    assert kept == saved and not outcome.changed and writes == []
    replaced, outcome, _ = apply(saved, {'carrier': 'config.api_key', 'operation': 'replace', 'entry_id': entry_id, 'value': 'replacement-key'})
    assert engine_admin.engine_view(load_engine_catalog(), replaced, db_path=tmp_path / 'admin.db')['key']['entry_id'] == entry_id
    assert outcome.retired == [saved['config']['api_key_secret_id']]
    cleared, outcome, _ = apply(saved, {'carrier': 'config.api_key', 'operation': 'clear', 'entry_id': entry_id})
    assert not cleared['config'].get('api_key_secret_id') and not cleared['config'].get('credential_origin')


def test_replacing_an_environment_key_removes_the_old_binding(tmp_path):
    saved = record(api_key_env='EDITOR_KEY', credential_origin='http://127.0.0.1:8080')
    shown = engine_admin.engine_view(load_engine_catalog(), saved, db_path=tmp_path / 'admin.db', env=lambda _: None)
    updated, _, writes = apply(saved, {'carrier': 'config.api_key', 'operation': 'replace', 'entry_id': shown['key']['entry_id'], 'value': 'pasted-editor-key'})
    assert 'api_key_env' not in updated['config'] and 'api_key_ref' not in updated['config']
    assert writes == [('pasted-editor-key', 'http://127.0.0.1:8080')]


def test_missing_stored_key_is_unavailable_without_decryption(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('a management view must never decrypt')
    monkeypatch.setattr(secret_store, 'get_provider_secret', forbidden)
    shown = engine_admin.engine_view(load_engine_catalog(), record(api_key_secret_id='missing-editor-key', credential_origin='http://127.0.0.1:8080'), db_path=tmp_path / 'admin.db')
    assert shown['key']['availability'] == 'unavailable'
    assert any(item['code'] == 'engine_credential_unavailable' for item in shown['attention'])


def test_protocol_dispatch_is_validated_and_persisted_as_connection_data():
    catalog = load_engine_catalog()
    definition = catalog.definition('openai-compatible')
    clean = engine_admin.clean_values(definition, {'address': 'http://127.0.0.1:8080', 'dispatch': 'protocol', 'protocol_description': 'openai-compatible'})
    raw = engine_admin.build_raw(catalog, definition, clean, name='Editor', capabilities=['chat'], models=[], enabled=False, check={}, tests={}, secret_id=None)
    assert raw['connection'] == {'dispatch': 'protocol', 'protocol': 'openai-compatible', 'address': 'http://127.0.0.1:8080', 'transport': 'http'}
    with pytest.raises(engine_admin.DraftError):
        engine_admin.clean_values(definition, {'address': 'http://127.0.0.1:8080', 'dispatch': 'protocol', 'protocol_description': 'no-such-installed-protocol'})


def test_engine_view_revision_is_the_same_snapshot_as_its_record(tmp_path):
    from gateway.admin_runtime import AdminSQLiteStore
    store = AdminSQLiteStore(tmp_path / 'admin.db')
    admin_store.init_db(store.path)
    admin_store.write_service_registry({'schema_version': 2, 'services': {'editor': record()}}, expected_generation=0, path=store.path)
    shown = engine_admin.EngineService(store).view('editor')
    assert shown['registry_generation'] == 1
    assert shown['values']['address'].startswith('http://127.0.0.1:8080')


def test_a_stale_retain_command_is_refused_before_any_secret_write(tmp_path):
    from gateway.admin_runtime import AdminContext, AdminSQLiteStore
    store = AdminSQLiteStore(tmp_path / 'admin.db')
    admin_store.init_db(store.path)
    saved = record(api_key_env='EDITOR_KEY', credential_origin='http://127.0.0.1:8080')
    admin_store.write_service_registry({'schema_version': 2, 'services': {'editor': saved}}, expected_generation=0, path=store.path)
    actor = AdminContext(subject='editor-owner', actor_kind='user', actor_id='editor-owner', actor_label='Owner', roles=('owner',))
    with pytest.raises(admin_store.OperatorSettingsGenerationConflict):
        store.apply_secret_commands(service_id='editor', commands=[{'carrier': 'config.api_key', 'operation': 'retain', 'entry_id': secret_commands.provider_key_entry_id(saved)}], actor=actor, expected_generation=0)

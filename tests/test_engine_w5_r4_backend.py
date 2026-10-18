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


def test_lifecycle_inference_sends_the_secret_header_value_instead_of_its_preview(tmp_path):
    from gateway import record_carriers, runtime_secrets, secret_refs
    from gateway.engine_connection import inference_connection
    path = tmp_path / 'admin.db'
    saved, _ = record_carriers.externalize_service(record(headers=[{'name': 'X-Tenant', 'value': 'private-runtime-tenant'}]), secret_refs.putter(path))
    runtime_secrets.configure(path)
    try:
        connection = inference_connection(saved, '/chat/completions', fallback_address='', fallback_timeout=600, db_path=path, env=lambda _: None)
        assert connection.headers['x-tenant'] == 'private-runtime-tenant'
    finally:
        runtime_secrets.configure(None)


def test_test_receipts_do_not_publish_an_unkeyed_digest_of_a_secret():
    import hashlib
    import json
    from gateway.engine_check import KeyInput
    definition = load_engine_catalog().definition('openai-compatible')
    values = {'address': 'http://127.0.0.1:8080'}
    key = KeyInput('short-key', 'pasted')
    snapshot = {'service_id': 'editor', 'definition': definition['definition'], 'connection': values,
                'credential': key.value, 'withheld': key.withheld, 'models': []}
    guessable = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert engine_admin._test_binding(definition, values, key, 'editor', []) != guessable


def test_engine_view_hides_unregistered_configuration_in_the_stored_record(tmp_path):
    saved = record(operator_extension='PRIVATE-UNKNOWN-CONFIG', extension_block={'password': 'PRIVATE-NESTED-CONFIG'})
    shown = engine_admin.engine_view(load_engine_catalog(), saved, db_path=tmp_path / 'admin.db')
    assert 'PRIVATE-UNKNOWN-CONFIG' not in str(shown)
    assert 'PRIVATE-NESTED-CONFIG' not in str(shown)
    assert shown['record']['config']['host'] == '127.0.0.1'
    assert shown['hidden_settings'] == 2


def test_stored_record_never_synthesizes_a_lifecycle_secret_carrier(tmp_path):
    saved = record()
    saved.pop('lifecycle', None)
    saved['config'].pop('lifecycle', None)
    saved['process'] = {'driver': 'command', 'start': ['runner']}
    shown = engine_admin.engine_view(load_engine_catalog(), saved, db_path=tmp_path / 'admin.db')
    assert 'lifecycle' not in shown['record']
    assert shown['record']['process']['start'] == ['runner']


def test_declared_connection_projects_credentials_and_hides_unknown_nested_fields(tmp_path):
    saved = record(connection={
        'dispatch': 'protocol', 'protocol': 'openai-compatible', 'transport': 'http',
        'address': 'http://user:INLINE-CREDENTIAL@127.0.0.1:8080?private=INLINE-QUERY',
        'neutral_extension': 'NESTED-CONNECTION-CANARY',
        'credentials': {'inference': {'secret_ref': 'opaque-private-reference', 'header': 'Authorization', 'scheme': 'Bearer',
                                      'origin': 'http://127.0.0.1:8080', 'neutral': 'NESTED-CREDENTIAL-CANARY'}},
    })
    shown = engine_admin.engine_view(load_engine_catalog(), saved, db_path=tmp_path / 'admin.db')
    for canary in ('INLINE-CREDENTIAL', 'INLINE-QUERY', 'NESTED-CONNECTION-CANARY', 'opaque-private-reference', 'NESTED-CREDENTIAL-CANARY'):
        assert canary not in str(shown)
    connection = shown['record']['config']['connection']
    assert connection['dispatch'] == 'protocol' and connection['credentials']['inference']['secret_ref'] == {'configured': False}


@pytest.mark.parametrize('address', ['http://user:private@127.0.0.1:8080', 'http://127.0.0.1:8080?key=private', 'http://127.0.0.1:8080#private'])
def test_protocol_root_requires_a_public_endpoint(address):
    with pytest.raises(engine_admin.DraftError):
        engine_admin.clean_values(load_engine_catalog().definition('openai-compatible'), {
            'address': address, 'dispatch': 'protocol', 'protocol_description': 'openai-compatible',
        })


def test_retain_response_does_not_pair_old_entries_with_a_new_revision(tmp_path, monkeypatch):
    from gateway.admin_runtime import AdminContext, AdminSQLiteStore
    store = AdminSQLiteStore(tmp_path / 'admin.db')
    admin_store.init_db(store.path)
    saved = record(api_key_env='EDITOR_KEY', credential_origin='http://127.0.0.1:8080')
    admin_store.write_service_registry({'schema_version': 2, 'services': {'editor': saved}}, expected_generation=0, path=store.path)
    snapshot = admin_store.get_service_registry_with_generation(path=store.path)
    later = copy.deepcopy(snapshot)
    later['operator_settings_generation'] += 1
    later['value']['services']['editor']['name'] = 'Changed elsewhere'
    calls = []
    def changing_snapshot(**kwargs):
        calls.append(True)
        return copy.deepcopy(snapshot if len(calls) == 1 else later)
    monkeypatch.setattr(admin_store, 'get_service_registry_with_generation', changing_snapshot)
    actor = AdminContext(subject='editor-owner', actor_kind='user', actor_id='editor-owner', actor_label='Owner', roles=('owner',))
    result = store.apply_secret_commands(service_id='editor', commands=[{'carrier': 'config.api_key', 'operation': 'retain', 'entry_id': secret_commands.provider_key_entry_id(saved)}], actor=actor, expected_generation=1)
    assert result['operator_settings_generation'] == snapshot['operator_settings_generation']
    assert result['value']['services']['editor']['name'] == snapshot['value']['services']['editor']['name']


def test_protocol_key_command_supplies_runtime_inference_credentials_and_clear_keeps_control(tmp_path, monkeypatch):
    from gateway import secret_refs
    from gateway.engine_credentials import credential_headers
    path = tmp_path / 'admin.db'
    monkeypatch.setattr(admin_store, 'DB_PATH', path)
    origin = 'http://127.0.0.1:8080'
    secret_store.put_provider_secret('control-only', 'control-credential', db_path=path, approved_origin=origin)
    saved = record(connection={'dispatch': 'protocol', 'protocol': 'openai-compatible', 'address': origin,
                               'credentials': {'control': {'secret_ref': 'control-only', 'origin': origin, 'header': 'X-Control'}}})
    configured, _ = secret_commands.apply_commands(saved, [{'carrier': 'config.api_key', 'operation': 'add', 'value': 'inference-credential'}], secret_refs.putter(path))
    assert credential_headers(configured, 'inference') == {'Authorization': 'Bearer inference-credential'}
    assert credential_headers(configured, 'control') == {'X-Control': 'control-credential'}
    cleared, _, _ = apply(configured, {'carrier': 'config.api_key', 'operation': 'clear', 'entry_id': secret_commands.provider_key_entry_id(configured)})
    assert credential_headers(cleared, 'inference') == {}
    assert credential_headers(cleared, 'control') == {'X-Control': 'control-credential'}


def test_opting_into_protocol_routes_existing_model_rows():
    from gateway.engine_protocol.dispatch import resolve
    saved = record(connection={'dispatch': 'protocol', 'protocol': 'openai-compatible', 'address': 'http://127.0.0.1:8080'})
    target = resolve({'services': {'editor': saved}}, 'editor-chat', 'chat')
    assert target is not None and target.upstream == 'test'


def test_connection_secret_commands_invalidate_saved_check_and_tests():
    saved = record(engine_check={'status': 'ok', 'checked_at': 123}, engine_tests={'chat': {'ok': True, 'at': 123}})
    changed, _, _ = apply(saved, {'carrier': 'config.headers', 'operation': 'add', 'name': 'X-Tenant', 'value': 'new-private-tenant'})
    assert not changed['config'].get('engine_check') and not changed['config'].get('engine_tests')


@pytest.mark.parametrize('reverse', [False, True])
def test_an_origin_change_cannot_share_a_secret_batch_with_a_new_key(reverse):
    saved = record(address='http://127.0.0.1:8080/v1')
    commands = [{'carrier': 'config.address', 'operation': 'set_credentials', 'url': 'http://user:private@127.0.0.1:9090/v1'},
                {'carrier': 'config.api_key', 'operation': 'add', 'value': 'new-provider-key'}]
    if reverse:
        commands.reverse()
    writes = []
    def put(value, origin):
        writes.append((value, origin))
        return 'sref_' + str(len(writes)).zfill(24)
    with pytest.raises(secret_commands.CommandError):
        secret_commands.apply_commands(saved, secret_commands.parse_commands({'commands': commands}), put)
    assert writes == []


def test_incompatible_operation_is_refused_before_a_valid_batch_item_can_store_a_secret():
    writes = []
    def put(value, origin):
        writes.append(True)
        return 'sref_000000000000000000000001'
    request = {'commands': [
        {'carrier': 'config.headers', 'operation': 'add', 'name': 'X-First', 'value': 'private-first'},
        {'carrier': 'config.headers', 'operation': 'set', 'items': [], 'clear': []},
    ]}
    with pytest.raises(secret_commands.CommandError):
        secret_commands.apply_commands(record(), secret_commands.parse_commands(request), put)
    assert writes == []

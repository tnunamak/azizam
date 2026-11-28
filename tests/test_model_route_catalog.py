"""Route publication and cache derivation use one snapshot and no provider calls."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import registry_snapshot, subscription_store
from tests.support import access as acc
from tests.support.fake_upstream.route_dispatch import request_for
from tests.test_model_route_dispatch import graph, harness  # noqa: F401


def build(services, **kwargs):
    return registry_snapshot.build({'services': services}, registry_snapshot.BundledRelease({}), generation=4, **kwargs)


def connection_service():
    return {'plan-source': {'id': 'plan-source', 'kind': 'provider', 'enabled': True, 'config': {
        'credential_source': {'kind': 'connection', 'connection_id': 'test-owner'},
        'request_protocol': 'openai-responses-plan', 'billing_mode': 'plan', 'models': []}}}


@pytest.mark.parametrize('connected', [True, False])
def test_discovery_derives_rows_and_pins_without_mutating_source(connected):
    services = connection_service()
    original = copy.deepcopy(services)
    cache = {'test-owner': {'models': {'alpha': ['low'], 'beta': ['high']}, 'status': 'connected' if connected else 'disconnected'}}
    snapshot = build(services, connection_models=cache)
    rows = snapshot.services['plan-source']['config']['models']
    assert [row['id'] for row in rows] == ['alpha', 'beta']
    assert set(snapshot.model_routes.pins) == {'plan-source/alpha', 'plan-source/beta'}
    assert snapshot.model_routes.pins['plan-source/alpha'].target == ('plan-source', 'alpha')
    assert rows[0]['reasoning_efforts'] == ('low',)
    assert services == original


def test_owner_record_wins_whole_and_refresh_drops_only_derived_rows():
    services = connection_service()
    owner = {'id': 'alpha', 'upstream_model': 'owner-wire', 'routes': [
        {'id': 'owner-pin', 'service_id': 'plan-source', 'model_id': 'alpha'}]}
    services['plan-source']['config']['models'] = [owner]
    first = build(services, connection_models={'test-owner': {'models': {'alpha': [], 'beta': []}}})
    second = build(services, connection_models={'test-owner': {'models': {'gamma': []}}})
    assert first.services['plan-source']['config']['models'][0]['upstream_model'] == 'owner-wire'
    assert set(first.model_routes.pins) == {'owner-pin', 'plan-source/beta'}
    assert set(second.model_routes.pins) == {'owner-pin', 'plan-source/gamma'}
    assert set(first.model_routes.pins) == {'owner-pin', 'plan-source/beta'}  # held snapshot stays immutable


def test_discovery_cache_reader_is_read_only_and_contains_no_connection_identity(tmp_path):
    import sqlite3
    path = tmp_path / 'subscriptions.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE subscription_connections (connection_id, kind, models_json, status, account_email, client_id)')
        db.execute('INSERT INTO subscription_connections VALUES (?,?,?,?,?,?)',
                   ('test-owner', 'test-kind', '{"alpha":["low"]}', 'disconnected', 'private@example.test', 'private-client'))
    before = path.read_bytes()
    result = subscription_store.cached_model_discovery(path)
    assert result == {'test-owner': {'models': {'alpha': ['low']}, 'status': 'disconnected'}}
    assert path.read_bytes() == before
    assert subscription_store.cached_model_discovery(tmp_path / 'missing.db') == {}
    assert not (tmp_path / 'missing.db').exists()


@pytest.fixture
def catalog(monkeypatch):
    services = graph()
    state = SimpleNamespace(services=services, identity=acc.identity(acc.plan_for(connections=['chatgpt-owner'])), ready=True,
                            discoveries=None)
    monkeypatch.setattr(proxy, 'ADMIN_STORE', SimpleNamespace(path=proxy.ADMIN_STORE.path, get_service_registry=lambda: {'services': state.services}))
    monkeypatch.setattr(proxy, '_enforce_runtime_route_policy', lambda *a, **kw: None)
    monkeypatch.setattr(proxy, '_litellm_runtime_config', lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr(proxy, '_protocol_models_payload', AsyncMock(return_value=[]))
    monkeypatch.setattr(proxy, '_build_workflow_model_entries', lambda: [])
    monkeypatch.setattr(proxy, '_current_loaded_model_entries', lambda: [])
    monkeypatch.setattr(proxy, '_stable_alias_model_entries', lambda: [])
    monkeypatch.setattr(proxy, '_decision_models_for', lambda *a: [])
    monkeypatch.setattr(proxy, '_annotate_model_context_windows', AsyncMock())
    monkeypatch.setattr(proxy, '_annotate_model_availability', lambda data, **kw: data)
    monkeypatch.setattr(proxy.SUBSCRIPTIONS, 'configured', lambda cid: state.ready)
    monkeypatch.setattr(proxy.providers, 'provider_refs', lambda registry: {'cloud': SimpleNamespace(configured=True)})
    def listed(query=''):
        snapshot = build(state.services, connection_models=state.discoveries)
        monkeypatch.setattr(proxy, 'GATEWAY_REGISTRY_PROVIDER', registry_snapshot.Provider(snapshot))
        with acc.authenticated(state.identity):
            response = TestClient(proxy.app).get('/v1/models' + query)
        assert response.status_code == 200, response.text
        return {row['id']: row for row in response.json()['data']}
    state.listed = listed
    return state


def test_http_publishes_one_logical_id_and_named_overlay_hides_pins(catalog):
    listed = catalog.listed()
    assert set(listed) == {'logical-test', 'speed-test'}
    assert [route['id'] for route in listed['logical-test']['azizam_routes']] == ['chatgpt-plan/gpt-test']
    assert listed['logical-test']['azizam_availability']['state'] == 'available'
    assert 'connection_id' not in json.dumps(listed) and 'paid/gpt-test' not in json.dumps(listed)


def test_http_pin_only_grant_never_publishes_the_logical_id(catalog):
    catalog.identity = acc.identity(acc.plan_for(models=['chatgpt-plan/gpt-test'], connections=['chatgpt-owner']))
    assert catalog.listed() == {}


def test_http_disconnected_first_route_keeps_other_authorized_route_available(catalog):
    catalog.ready = False
    catalog.identity = acc.identity(acc.plan_for(connections=['chatgpt-owner'], paid=['cloud']))
    listed = catalog.listed('?available=true')
    assert listed['logical-test']['azizam_availability']['state'] == 'available'
    assert [route['id'] for route in listed['logical-test']['azizam_routes']] == ['chatgpt-plan/gpt-test', 'paid/gpt-test']


def test_http_disconnected_only_route_stays_listed_unavailable(catalog):
    catalog.ready = False
    assert catalog.listed()['logical-test']['azizam_availability']['state'] == 'unavailable'
    assert catalog.listed('?available=true') == {}


@pytest.mark.parametrize('model', ['logical-test', 'chatgpt-plan/gpt-test'])
@pytest.mark.parametrize('status', [200, 429, 503])
def test_chat_route_names_bypass_openai_patterns_and_alias_fallback(monkeypatch, model, status):
    snapshot = build(graph())
    auth = proxy.AuthContext(identity=acc.identity(acc.plan_for(connections=['chatgpt-owner'])))
    monkeypatch.setattr(proxy, 'authenticate_runtime_request', lambda *a: SimpleNamespace(auth_context=auth))
    def no_alias(*a, **kw):
        raise AssertionError('a route group or pin must never reach OpenAI-name aliases or their fallback engines')
    monkeypatch.setattr(proxy, 'resolve_stable_alias', no_alias)
    response = proxy.JSONResponse({'chosen': model}, status_code=status)
    dispatch = AsyncMock(return_value=response)
    monkeypatch.setattr(proxy, '_model_route_response', dispatch)
    request = request_for({'model': model, 'messages': [{'role': 'user', 'content': 'hi'}]}, surface='chat')
    request.state.registry_snapshot = snapshot
    result = asyncio.run(proxy._handle_openai_litellm_request(request, '/v1/chat/completions'))
    assert result is response
    assert dispatch.await_count == 1


def test_collision_keeps_plan_pin_private_and_does_not_join_owner_routes():
    services = connection_service()
    services['owner'] = {'config': {'models': [{'id': 'alpha', 'upstream_model': 'owner-model'}]}}
    snapshot = build(services, connection_models={'test-owner': {'models': {'alpha': []}}})
    derived = snapshot.services['plan-source']['config']['models'][0]
    assert derived['execution_only'] is True
    assert snapshot.model_routes.pins['plan-source/alpha'].target == ('plan-source', 'alpha')
    assert len(snapshot.model_routes.by_model['owner', 'alpha']) == 1
    assert snapshot.model_routes.by_model['owner', 'alpha'][0].service_id == 'owner'


def test_pin_inspection_works_for_pin_only_grant(catalog):
    catalog.identity = acc.identity(acc.plan_for(models=['chatgpt-plan/gpt-test'], connections=['chatgpt-owner']))
    catalog.listed()
    with acc.authenticated(catalog.identity):
        response = TestClient(proxy.app).get('/v1/models/chatgpt-plan/gpt-test')
    assert response.status_code == 200, response.text
    assert [route['id'] for route in response.json()['azizam_routes']] == ['chatgpt-plan/gpt-test']


def test_catalog_guarantees_intersect_only_the_visible_eligible_routes(catalog):
    catalog.services['plan-source']['config']['models'][0].update(capabilities=['chat', 'vision'], input_modalities=['text', 'image'])
    catalog.services['cloud']['config']['models'][0].update(capabilities=['chat'], input_modalities=['text'], streaming=False)
    assert catalog.listed()['logical-test']['capabilities'] == ['chat', 'vision']
    catalog.identity = acc.identity(acc.plan_for(connections=['chatgpt-owner'], paid=['cloud']))
    listed = catalog.listed()['logical-test']
    assert listed['capabilities'] == ['chat'] and listed['architecture']['input_modalities'] == ['text']
    assert listed['streaming'] is False


def _member_catalog_ids(catalog, monkeypatch):
    catalog.listed()
    monkeypatch.setattr(proxy, '_oidc_session_from_request', lambda request: object())
    monkeypatch.setattr(proxy, 'build_user_portal_snapshot', lambda *a, **kw: {'authenticated': True, 'user': {}})
    monkeypatch.setattr(proxy, '_session_catalog_auth_contexts', lambda *a: [proxy.AuthContext(identity=catalog.identity)])
    monkeypatch.setattr(proxy, 'build_user_activity_snapshot', lambda *a, **kw: {'receipts': []})
    monkeypatch.setattr(proxy, '_gateway_capabilities_for_auth_context', lambda *a: [])
    monkeypatch.setattr(proxy, '_live_probe_evidence', AsyncMock(return_value={}))
    monkeypatch.setattr(proxy, '_models_catalog_health_evidence', AsyncMock(return_value={}))
    monkeypatch.setattr(proxy, '_status_payload_with_service_rows', AsyncMock(return_value={}))
    monkeypatch.setattr(proxy, '_runtime_availability_evidence', lambda *a, **kw: {})
    monkeypatch.setattr(proxy, '_models_catalog_from', lambda *a, **kw: {
        'aliases': [], 'models': [], 'counts': {'aliases': 0, 'models': 0, 'capabilities': 0}})
    result = asyncio.run(proxy._user_models_catalog_payload(request_for({})))
    return result, {row['id'] for row in result['models']}


def test_member_models_surface_uses_the_same_logical_catalog(catalog, monkeypatch):
    result, ids = _member_catalog_ids(catalog, monkeypatch)
    assert ids == {'logical-test', 'speed-test'}
    assert result['counts']['models'] == 2


def test_plain_paid_singleton_keeps_its_legacy_executor(monkeypatch):
    services = {'cloud': {'id': 'cloud', 'kind': 'provider', 'enabled': True, 'config': {
        'provider_id': 'cloud', 'remote_provider': True, 'models': [{'id': 'plain', 'upstream_model': 'wire'}]}}}
    snapshot = build(services)
    request = request_for({'model': 'cloud/plain'})
    request.state.registry_snapshot = snapshot
    result = asyncio.run(proxy._model_route_response(request, proxy.AuthContext(identity=acc.identity(acc.plan_for(paid=['cloud']))),
                                                    {'model': 'cloud/plain'}, surface='chat'))
    assert result is None


def test_local_effort_400_is_terminal_even_if_a_paid_route_accepts_it(harness):
    harness.service.store.set_models("chatgpt", {"gpt-test": ["low"]})
    response = asyncio.run(harness.call(body={"model": "logical-test", "input": [{"role": "user", "content": "hi"}],
                                             "reasoning": {"effort": "high"}}))
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["param"] == "reasoning.effort"
    assert not harness.paid_calls and not harness.reservations
    assert harness.upstream.count("/v1/responses") == 0


LIVE_SLUGS = ('gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-6-astra', 'gpt-6-luna',
              'gpt-6-sol', 'gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6.2-astra', 'gpt-6.2-sol')


def test_owner_supplied_discovery_names_publish_as_bare_ids_and_keep_exact_pins(monkeypatch):
    services = connection_service()
    snapshot = build(services, connection_models={'test-owner': {'models': {mid: [] for mid in LIVE_SLUGS}}})
    monkeypatch.setattr(proxy.SUBSCRIPTIONS, 'configured', lambda cid: True)
    monkeypatch.setattr(proxy, '_operator_service_availability_overrides', lambda: {})
    entries, _ = proxy._model_route_catalog(snapshot, proxy.AuthContext(identity=acc.identity(acc.plan_for(connections=['test-owner']))))
    assert {entry['id'] for entry in entries} == set(LIVE_SLUGS)
    assert set(snapshot.model_routes.pins) == {f'plan-source/{mid}' for mid in LIVE_SLUGS}


def test_refresh_changes_next_snapshot_and_disconnect_keeps_derived_rows(tmp_path, monkeypatch):
    from tests.support.subscription_fakes import KIND, Upstream, connect, make_service
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    asyncio.run(connect(service, jwks, upstream))
    services = connection_service()
    services['plan-source']['config']['credential_source']['connection_id'] = 'chatgpt-owner'
    def next_snapshot():
        return build(services, connection_models=subscription_store.cached_model_discovery(tmp_path / 'subscriptions.db'))
    provider = registry_snapshot.Provider(next_snapshot())
    held = provider.current()
    service.snapshot = provider.current
    service.changed = lambda: provider.swap(next_snapshot())
    monkeypatch.setattr(service, '_fetch_models', AsyncMock(return_value={'refreshed': ['low']}))
    asyncio.run(service.refresh_models(KIND.id))
    assert set(held.model_routes.pins) == {'plan-source/gpt-test'}
    assert set(provider.current().model_routes.pins) == {'plan-source/refreshed'}
    status = asyncio.run(service.status_one(KIND.id))
    assert status['models'] == ['refreshed']
    assert status['routes'] == [{'id': 'plan-source/refreshed', 'logical_model': 'refreshed',
                                 'service_id': 'plan-source', 'model_id': 'refreshed'}]
    asyncio.run(service.disconnect(KIND.id, session=None))
    assert not service.configured('chatgpt-owner')
    assert set(next_snapshot().model_routes.pins) == {'plan-source/refreshed'}
    assert services['plan-source']['config']['models'] == []


def test_catalog_omits_an_edge_that_cannot_serve_chat(catalog):
    catalog.services['plan-source']['capabilities'] = ['image']
    assert catalog.listed() == {}


def test_owner_connection_row_without_routes_publishes_its_singleton(monkeypatch):
    services = connection_service()
    services['plan-source']['config']['models'] = [{'id': 'alpha', 'upstream_model': 'owner-alpha'}]
    snapshot = build(services, connection_models={'test-owner': {'models': {'alpha': []}}})
    monkeypatch.setattr(proxy.SUBSCRIPTIONS, 'configured', lambda cid: True)
    entries, _ = proxy._model_route_catalog(snapshot, proxy.AuthContext(identity=acc.identity(acc.plan_for(connections=['test-owner']))))
    assert [entry['id'] for entry in entries] == ['alpha']
    assert entries[0]['azizam_routes'][0]['id'] == 'plan-source/alpha'


def test_owner_save_refuses_cache_only_target_with_the_field_to_add():
    from gateway import service_registry
    from tests.test_model_routes import graph as owner_graph
    services = owner_graph()
    services['cloud']['config'].update(credential_source={'kind': 'connection', 'connection_id': 'test-owner'}, models=[])
    with pytest.raises(ValueError) as caught:
        service_registry.normalize_service_registry({'services': services})
    assert 'discovered from the connection' in str(caught.value)
    assert 'services.cloud.config.models' in str(caught.value)
    assert 'add the target row first' in str(caught.value)


def test_owner_target_row_overrides_discovery_and_compiles_ordered_group(tmp_path, monkeypatch):
    from gateway import admin_store, service_registry
    from tests.test_model_routes import graph as owner_graph
    services = owner_graph()
    services['cloud']['config']['credential_source'] = {'kind': 'connection', 'connection_id': 'test-owner'}
    document = service_registry.normalize_service_registry({'services': services})
    record = {'value_json': json.dumps(document), 'operator_settings_generation': 7}
    monkeypatch.setattr(admin_store, 'get_service_registry_json_with_generation', lambda **kw: record)
    monkeypatch.setattr(service_registry, 'release_registry', lambda: {})
    monkeypatch.setattr(subscription_store, 'cached_model_discovery', lambda path: {
        'test-owner': {'models': {'other': ['low']}, 'status': 'disconnected'}})
    monkeypatch.setattr(registry_snapshot, 'bundled_release', lambda: registry_snapshot.BundledRelease({}))
    snapshot = registry_snapshot.load_current(tmp_path / 'admin.db')
    assert [edge.id for edge in snapshot.model_routes.by_model['anchor', 'logical']] == [
        'anchor/logical', 'cloud/wire', 'broker/wire']
    row = snapshot.services['cloud']['config']['models'][0]
    assert row['upstream_model'] == 'wire-b' and row['execution_only'] is True
    assert 'routes' not in row and 'reasoning_efforts' not in row
    assert snapshot.revision == 7


# ------------------------------------------------------------------ derivation never stops the registry from building

def _derived_names(snapshot):
    return {row['id'] for row in snapshot.services['plan-source']['config'].get('models', ())}


def _owner_models(snapshot):
    return [dict(row) for row in snapshot.services['owner']['config']['models']]


def test_derived_row_matching_an_owner_upstream_only_row_is_skipped():
    services = connection_service()
    services['owner'] = {'config': {'models': [{'upstream_model': 'alpha'}]}}
    snapshot = build(services, connection_models={'test-owner': {'models': {'alpha': [], 'beta': []}}})
    assert _derived_names(snapshot) == {'beta'}
    assert _owner_models(snapshot) == services['owner']['config']['models']


def test_derived_row_matching_an_owner_pin_id_is_skipped():
    services = connection_service()
    services['owner'] = {'config': {'models': [{'id': 'plan-source/alpha', 'upstream_model': 'owner-wire'}]}}
    snapshot = build(services, connection_models={'test-owner': {'models': {'alpha': [], 'beta': []}}})
    assert _derived_names(snapshot) == {'beta'}
    assert _owner_models(snapshot) == services['owner']['config']['models']


@pytest.mark.parametrize('efforts', [None, 'low', [1]])
def test_derived_row_with_malformed_efforts_is_skipped(efforts):
    services = connection_service()
    snapshot = build(services, connection_models={'test-owner': {'models': {'alpha': efforts, 'beta': ['low']}}})
    assert _derived_names(snapshot) == {'beta'}


def test_derived_row_matching_a_workflow_name_is_skipped():
    from tests.test_registry_snapshot import CONTRACT, bundled_of
    services = connection_service()
    services['owner'] = {'config': {'models': [{'id': 'owner-model', 'upstream_model': 'owner-wire'}]}}
    slug = CONTRACT['id']
    snapshot = registry_snapshot.build({'services': services}, bundled_of(CONTRACT), generation=4,
                                       connection_models={'test-owner': {'models': {slug: [], 'beta': []}}})
    assert _derived_names(snapshot) == {'beta'}
    assert slug in {entry.id for entry in snapshot.workflows.values()}
    assert _owner_models(snapshot) == services['owner']['config']['models']


def test_derived_collision_logs_connection_and_slug_only(caplog):
    services = connection_service()
    services['owner'] = {'config': {'models': [{'upstream_model': 'alpha'}]}}
    with caplog.at_level('WARNING', logger='gateway.registry_snapshot'):
        build(services, connection_models={'test-owner': {'models': {'alpha': []}}})
    lines = [record.getMessage() for record in caplog.records if 'not derived' in record.getMessage()]
    assert lines == ["connection model not derived: connection test-owner, model 'alpha' names an existing public entity"]


def test_candidate_snapshot_includes_derived_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, 'ADMIN_DB_PATH', tmp_path / 'admin.db')
    monkeypatch.setattr(subscription_store, 'cached_model_discovery', lambda path: (
        {'test-owner': {'models': {'alpha': []}}} if path == tmp_path / 'subscriptions.db' else {}))
    monkeypatch.setattr(proxy.registry_snapshot, 'bundled_release', lambda: registry_snapshot.BundledRelease({}))
    snapshot = proxy._workflow_store_snapshot({'services': connection_service()}, None, generation=2)
    assert _derived_names(snapshot) == {'alpha'}


# ------------------------------------------------------------------ a plan without the connection sees none of it

@pytest.fixture
def derived_catalog(catalog):
    catalog.services = connection_service()
    catalog.discoveries = {'test-owner': {'models': {'alpha': []}}}
    catalog.identity = acc.identity(acc.plan_for())
    return catalog


def test_ungranted_plan_lists_no_derived_names(derived_catalog):
    assert derived_catalog.listed() == {}
    assert derived_catalog.listed('?available=true') == {}
    derived_catalog.identity = acc.identity(acc.plan_for(connections=['test-owner']))
    assert set(derived_catalog.listed()) == {'alpha'}   # the same snapshot lists it once the connection is granted


def test_ungranted_plan_member_catalog_has_no_derived_names(derived_catalog, monkeypatch):
    _, ids = _member_catalog_ids(derived_catalog, monkeypatch)
    assert ids == set()


def test_ungranted_plan_cannot_inspect_a_derived_pin(derived_catalog):
    derived_catalog.listed()
    with acc.authenticated(derived_catalog.identity):
        client = TestClient(proxy.app)
        assert client.get('/v1/models/plan-source/alpha').status_code == 404
        assert client.get('/v1/models/alpha').status_code == 404


@pytest.mark.parametrize('model', ['alpha', 'plan-source/alpha'])
def test_ungranted_plan_is_refused_on_chat_with_the_bare_name_and_the_pin(derived_catalog, monkeypatch, model):
    derived_catalog.listed()
    from gateway import receipt_store, subscription_execution
    def never_sent(*args, **kwargs):
        raise AssertionError('an ungranted plan must not reach a plan attempt')
    monkeypatch.setattr(subscription_execution, 'execute_route_attempt', never_sent)
    monkeypatch.setattr(receipt_store, 'begin', lambda row: None)
    monkeypatch.setattr(receipt_store, 'finalize', lambda row: None)
    monkeypatch.setattr(receipt_store, 'update_phases', lambda row: None)
    monkeypatch.setattr(receipt_store, 'touch', lambda row: None)
    with acc.authenticated(derived_catalog.identity):
        response = TestClient(proxy.app).post('/v1/chat/completions', json={
            'model': model, 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert response.status_code == 403, response.text
    assert response.json()['error']['code'] == 'not_in_access_plan'

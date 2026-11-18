"""Owner access to service-scoped choice snapshots."""
import threading
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway.engine_protocol import choice_lists
from gateway.engine_protocol.choice_store import ChoiceStore
from gateway.workflow_owner_api import register_workflow_owner_routes


@pytest.fixture
def owner(tmp_path, monkeypatch):
    store = ChoiceStore(tmp_path / 'choices.json')
    monkeypatch.setattr(choice_lists, 'STORE', store)
    inputs = {name: {'choices_from': {'node_type': 'Node', 'input': name}}
              for name in ('fresh', 'stale', 'missing', 'failed')}
    entry = SimpleNamespace(id='demo', contract={'inputs': inputs}, bindings={'default': object()})
    registry = SimpleNamespace(snapshot=lambda: SimpleNamespace(workflows={'demo': entry}, get=lambda wid: entry if wid == 'demo' else None))
    role = {'value': 'owner'}
    def guard(request):
        if role['value'] != 'owner':
            return JSONResponse({'error': 'owner only'}, status_code=401 if role['value'] is None else 403)
        return object()
    router = APIRouter(prefix='/admin/api', include_in_schema=False)
    register_workflow_owner_routes(router, store=registry, guard=guard, read_json=lambda request: {},
        resolve_identity=lambda entry, binding_key=None: SimpleNamespace(service_id='svc', origin='http://203.0.113.10:8188'))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, store, role, entry


def test_owner_can_list_choice_freshness(owner, monkeypatch):
    client, store, _, _ = owner
    monkeypatch.setattr(choice_lists.time, 'time', lambda: 100000)
    store.write('svc', 'Node', 'fresh', ['a'], fetched_at=99990)
    store.write('svc', 'Node', 'stale', ['b'], fetched_at=1)
    store.write('svc', 'Node', 'failed', ['c'], fetched_at=1)
    store.record_failure('svc', 'Node', 'failed', 'unavailable', at=99999)
    rows = {r['input']: r for r in client.get('/admin/api/workflows/choices').json()['choices']}
    assert [rows[x]['state'] for x in ('fresh', 'stale', 'missing', 'failed')] == ['fresh', 'stale', 'missing', 'failed']
    assert rows['fresh']['age_s'] == 10
    assert rows['failed']['failure_reason'] == 'unavailable'


def test_owner_refresh_and_failure(owner, monkeypatch):
    client, store, _, _ = owner
    monkeypatch.setattr(choice_lists, 'fetch_fresh', lambda *args: {'input': {'required': {name: [['new']] for name in ('fresh', 'stale', 'missing', 'failed')}}})
    assert client.post('/admin/api/workflows/demo/choices/refresh').status_code == 200
    assert store.read('svc', 'Node', 'fresh').choices == ['new']
    def fail(*args):
        raise OSError('offline')
    monkeypatch.setattr(choice_lists, 'fetch_fresh', fail)
    assert client.post('/admin/api/workflows/choices/refresh').status_code == 200
    row = client.get('/admin/api/workflows/choices').json()['choices'][0]
    assert row['choices'] == ['new'] and row['state'] == 'failed' and 'offline' in row['failure_reason']


@pytest.mark.parametrize('role', ['member', 'app-key', None])
@pytest.mark.parametrize('method,path', [('get', '/admin/api/workflows/choices'), ('post', '/admin/api/workflows/choices/refresh')])
def test_owner_only(owner, role, method, path):
    client, _, roles, _ = owner
    roles['value'] = role
    assert getattr(client, method)(path).status_code == (401 if role is None else 403)


def test_owner_refresh_joins_background_refresh(owner, monkeypatch):
    client, _, _, entry = owner
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def fetch(*args):
        calls.append(args)
        entered.set()
        assert release.wait(10)
        return {'input': {'required': {name: [['new']] for name in ('fresh', 'stale', 'missing', 'failed')}}}
    monkeypatch.setattr(choice_lists, 'fetch_fresh', fetch)
    shield = choice_lists.asyncio.shield
    def observe_join(task):
        joined.set()
        return shield(task)
    monkeypatch.setattr(choice_lists.asyncio, 'shield', observe_join)
    identity = SimpleNamespace(service_id='svc', origin='http://203.0.113.10:8188')
    client.portal.call(choice_lists.choices_for_run, identity, entry.contract['inputs'])
    try:
        assert entered.wait(5)
        thread = threading.Thread(target=lambda: client.post('/admin/api/workflows/choices/refresh'))
        thread.start()
        assert joined.wait(5)
        assert len(calls) == 1
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and len(calls) == 1


def test_owner_routes_are_excluded_from_openapi(owner):
    client, _, _, _ = owner
    assert '/admin/api/workflows/choices' not in client.get('/openapi.json').json()['paths']


def test_unresolved_identity_is_a_per_input_failure(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(choice_lists, 'STORE', ChoiceStore(tmp_path / 'choices.json'))
    entry = SimpleNamespace(id='demo', contract={'inputs': {'pick': {'choices_from': {'node_type': 'Node', 'input': 'pick'}}}})
    def unavailable(entry):
        raise ValueError('unavailable')
    rows = asyncio.run(choice_lists.owner_choices([entry], unavailable, refresh=True))
    assert rows[0]['state'] == 'failed' and rows[0]['failure_reason'] == 'no bound service'


def test_missing_address_records_failure(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(choice_lists, 'STORE', ChoiceStore(tmp_path / 'choices.json'))
    entry = SimpleNamespace(id='demo', contract={'inputs': {'pick': {'choices_from': {'node_type': 'Node', 'input': 'pick'}}}})
    rows = asyncio.run(choice_lists.owner_choices([entry], lambda entry: SimpleNamespace(service_id='svc', origin=''), refresh=True))
    assert rows[0]['state'] == 'failed' and 'no service address' in rows[0]['failure_reason']


def test_joined_refresh_also_fetches_other_declarations(tmp_path, monkeypatch):
    import asyncio
    store = ChoiceStore(tmp_path / 'choices.json')
    monkeypatch.setattr(choice_lists, 'STORE', store)
    entered, release = threading.Event(), threading.Event()
    identity = SimpleNamespace(service_id='svc', origin='http://203.0.113.10:8188')
    entry = SimpleNamespace(id='demo', contract={'inputs': {
        'one': {'choices_from': {'node_type': 'One', 'input': 'one'}},
        'two': {'choices_from': {'node_type': 'Two', 'input': 'two'}}}})
    calls = []
    def fetch(host, node):
        calls.append(node)
        if node == 'One':
            entered.set()
            assert release.wait(10)
        return {'input': {'required': {node.lower(): [['new']]}}}
    monkeypatch.setattr(choice_lists, 'fetch_fresh', fetch)
    async def scenario():
        choice_lists.choices_for_run(identity, {'one': entry.contract['inputs']['one']})
        assert await asyncio.to_thread(entered.wait, 5)
        task = asyncio.create_task(choice_lists.owner_choices([entry], lambda entry: identity, refresh=True))
        try:
            while not task.done() and len(asyncio.all_tasks()) < 3:
                await asyncio.sleep(0)
        finally:
            release.set()
        await task
    asyncio.run(scenario())
    assert calls == ['One', 'Two']
    assert store.read('svc', 'Two', 'two').choices == ['new']

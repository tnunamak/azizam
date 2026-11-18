import asyncio
import threading
from types import SimpleNamespace as NS

import pytest

from gateway.engine_protocol import choice_lists as cl
from gateway.engine_protocol.choice_store import ChoiceStore
from tests.test_workflow_owner_api import api, create, owner_contract


def test_workflow_named_choices_is_still_readable(api):
    contract = owner_contract()
    contract['id'] = 'choices'
    response = create(api, contract=contract)
    assert response.status_code == 201, response.text
    assert 'workflow' in api.get('/admin/api/workflows/choices').json()


def test_failure_older_than_snapshot_is_not_current(tmp_path, monkeypatch):
    store = ChoiceStore(tmp_path / 'choices.json')
    monkeypatch.setattr(cl, 'STORE', store)
    monkeypatch.setattr(cl.time, 'time', lambda: 100)
    store.record_failure('svc', 'N', 'pick', 'old failure', at=10)
    store.write('svc', 'N', 'pick', ['good'], fetched_at=90)
    entry = NS(id='demo', bindings={'first': object()}, contract={'inputs': {'pick': {'choices_from': {'node_type': 'N', 'input': 'pick'}}}})
    rows = asyncio.run(cl.owner_choices([entry], lambda entry, binding_key=None: NS(service_id='svc', origin='http://203.0.113.10')))
    assert rows[0]['state'] == 'fresh'
    assert rows[0]['failure_reason'] == 'old failure' and rows[0]['failed_at'] == 10
    store.record_failure('svc', 'N', 'pick', 'new failure', at=95)
    rows = asyncio.run(cl.owner_choices([entry], lambda entry, binding_key=None: NS(service_id='svc', origin='http://203.0.113.10')))
    assert rows[0]['state'] == 'failed' and rows[0]['failure_reason'] == 'new failure'


def test_refresh_covers_second_runnable_binding(tmp_path, monkeypatch):
    store = ChoiceStore(tmp_path / 'choices.json')
    monkeypatch.setattr(cl, 'STORE', store)
    entry = NS(id='demo', bindings={'first': 'svc-a', 'second': 'svc-b'}, contract={'inputs': {'pick': {'choices_from': {'node_type': 'N', 'input': 'pick'}}}})
    def resolve(entry, binding_key=None):
        return NS(service_id=entry.bindings[binding_key or 'first'], origin='http://203.0.113.10')
    monkeypatch.setattr(cl, 'fetch_fresh', lambda *a: {'input': {'required': {'pick': [['good']]}}})
    rows = asyncio.run(cl.owner_choices([entry], resolve, refresh=True))
    assert store.read('svc-a', 'N', 'pick') is not None
    assert store.read('svc-b', 'N', 'pick') is not None
    assert {(r['binding_key'], r['service_id']) for r in rows} == {('first', 'svc-a'), ('second', 'svc-b')}


def test_owner_timeout_returns_refreshing_while_fetch_continues(tmp_path, monkeypatch):
    store = ChoiceStore(tmp_path / 'choices.json')
    monkeypatch.setattr(cl, 'STORE', store)
    monkeypatch.setattr(cl, 'OWNER_REFRESH_TIMEOUT', 0.05, raising=False)
    entry = NS(id='demo', bindings={'first': object()}, contract={'inputs': {'pick': {'choices_from': {'node_type': 'N', 'input': 'pick'}}}})
    entered, release = threading.Event(), threading.Event()
    def fetch(*args):
        entered.set()
        assert release.wait(5)
        return {'input': {'required': {'pick': [['good']]}}}
    monkeypatch.setattr(cl, 'fetch_fresh', fetch)
    async def scenario():
        try:
            task = asyncio.create_task(cl.owner_choices([entry], lambda entry, binding_key=None: NS(service_id='svc', origin='http://203.0.113.10'), refresh=True))
            assert await asyncio.to_thread(entered.wait, 2)
            rows = await asyncio.wait_for(task, 1)
            assert rows[0]['state'] == 'refreshing'
            assert 'svc' in cl._tasks and not cl._tasks['svc'].done()
        finally:
            release.set()
            await asyncio.gather(*tuple(cl._tasks.values()))
        assert store.read('svc', 'N', 'pick').choices == ['good']
    asyncio.run(scenario())

import asyncio
import copy
from dataclasses import replace
import threading

import pytest

from gateway.engine_protocol.choice_store import ChoiceStore
from gateway.workflow_runtime_types import RunJobView, RunRefused
from tests.test_workflow_run import CONTRACT, Caller, make_core, request
from tests.workflow_runtime_fakes import FakeSnapshot, entry
from types import MappingProxyType


def setup_run(tmp_path, monkeypatch):
    from gateway.engine_protocol import choice_lists
    store = ChoiceStore(tmp_path / 'choices.json')
    monkeypatch.setattr(choice_lists, 'STORE', store)
    contract = copy.deepcopy(CONTRACT)
    contract['inputs']['pick'] = {'type': 'string', 'enum': ['fallback'], 'choices_from': {'node_type': 'Node', 'input': 'pick'}}
    snap = FakeSnapshot(1, MappingProxyType({'demo.run': entry(contract)}))
    from gateway import media_jobs
    db = tmp_path / 'jobs.db'
    media_jobs.init_db(db)
    core, port, _ = make_core(db, tmp_path, snap)
    original_identity = port.adapter_identity
    monkeypatch.setattr(port, "adapter_identity", lambda *args: replace(original_identity(*args), origin="http://203.0.113.10:8188"))
    return choice_lists, store, core, snap


async def submit(core, snap, value, key):
    return await core.submit_run(request(inputs={'prompt': 'hello', 'pick': value}, idempotency_key=key), Caller(), snapshot=snap)


@pytest.mark.parametrize('service,value,accepted', [('svc', 'new', True), ('svc', 'invalid', False), ('other', 'new', False), ('other', 'fallback', True)])
def test_snapshot_is_scoped_to_bound_service(tmp_path, monkeypatch, service, value, accepted):
    choices, store, core, snap = setup_run(tmp_path, monkeypatch)
    store.write(service, 'Node', 'pick', ['new'], fetched_at=10**10)
    monkeypatch.setattr(choices, 'fetch_fresh', lambda *a: {'input': {'required': {'pick': [['new']]}}})
    result = asyncio.run(submit(core, snap, value, 'key'))
    assert isinstance(result, RunJobView if accepted else RunRefused)
    if not accepted:
        assert result.code == 'invalid_inputs'


def test_first_run_refresh_is_single_flight(tmp_path, monkeypatch):
    choices, store, core, snap = setup_run(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fetch(host, node):
        calls.append((host, node))
        entered.set()
        assert release.wait(10)
        return {'input': {'required': {'pick': [['new']]}}}
    monkeypatch.setattr(choices, 'fetch_fresh', fetch)
    async def scenario():
        try:
            assert isinstance(await submit(core, snap, 'fallback', 'one'), RunJobView)
            assert await asyncio.to_thread(entered.wait, 5)
            assert isinstance(await asyncio.wait_for(submit(core, snap, 'fallback', 'two'), 2), RunJobView)
            assert calls == [("http://203.0.113.10:8188", "Node")]
        finally:
            release.set()
        while choices._tasks:
            await asyncio.gather(*tuple(choices._tasks.values()))
        assert isinstance(await submit(core, snap, 'new', 'three'), RunJobView)
    asyncio.run(scenario())


def test_failure_retains_snapshot(tmp_path, monkeypatch):
    choices, store, core, snap = setup_run(tmp_path, monkeypatch)
    store.write('svc', 'Node', 'pick', ['old'], fetched_at=1)
    def fail(*args):
        raise OSError('unavailable')
    monkeypatch.setattr(choices, 'fetch_fresh', fail)
    async def scenario():
        assert isinstance(await submit(core, snap, 'old', 'one'), RunJobView)
        while choices._tasks:
            await asyncio.gather(*tuple(choices._tasks.values()))
        assert store.read_failure('svc', 'Node', 'pick') is not None
        assert isinstance(await submit(core, snap, 'old', 'two'), RunJobView)
        while choices._tasks:
            await asyncio.gather(*tuple(choices._tasks.values()))
    asyncio.run(scenario())


def test_identity_failure_keeps_previous_creation_failure(tmp_path, monkeypatch):
    choices, store, core, snap = setup_run(tmp_path, monkeypatch)
    calls = []
    def fail(*args):
        calls.append(1)
        raise ValueError('binding_unavailable')
    monkeypatch.setattr(core._port, 'adapter_identity', fail)
    with pytest.raises(ValueError, match='binding_unavailable'):
        asyncio.run(submit(core, snap, 'fallback', 'identity'))
    assert len(calls) == 2
    assert not choices._tasks
    assert not store.path.exists()

"""Synthetic service records and wire recorder for remote protocol boundary tests."""
import asyncio
import copy
import json
from pathlib import Path

import httpx

from gateway.engine_protocol import description, dispatch
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.transport import HttpxTransport
from tests.support.fake_upstream import HandlerTransport


def case():
    from gateway.remote_engine import ClientFactory
    import yaml

    raw = yaml.safe_load((Path(__file__).parents[1] / 'fixtures/engine_protocol/d0/smell.yml').read_text())
    raw['auth'] = {'inference': {'kind': 'bearer'}, 'control': {'kind': 'bearer'}}
    for name, cls, method, path in [('models', 'inventory', 'GET', '/models'),
                                     ('select', 'control', 'POST', '/select'),
                                     ('state', 'control', 'GET', '/state')]:
        raw['operations'][name] = {'class': cls, 'request': {'method': method, 'path': path},
                                   'response': {'framing': 'json', 'select': {'value': '/value'}}}
    doc = description.parse_description(raw, source='remote fixture')
    offering = {'id': 'remote-model', 'upstream': 'remote-model', 'kind': 'model',
                'tasks': [{'task': 'smell.infer'}], 'selection': {'mode': 'request_field'}}
    service = {'id': 'remote-box', 'enabled': True, 'config': {
        'connection': {'dispatch': 'protocol', 'protocol': doc.id, 'address': 'https://engine.example',
                       'remote_dispatch': {'version': 1, 'enabled': True}, 'credentials': {}},
        'offerings': [offering]}}
    registry = {'services': {'remote-box': service}}
    requests = []
    selected = []

    def handler(request):
        requests.append(request)
        if request.url.path == '/v1/infer':
            return httpx.Response(200, json={'job': {'id': 'j'}})
        if request.url.path == '/v1/infer/j':
            return httpx.Response(200, json={'state': 'ready'})
        if request.url.path.endswith('/molecule'):
            return httpx.Response(200, content=b'remote-result', headers={'Content-Type': 'application/octet-stream'})
        if request.url.path == '/select':
            selected.append(True)
        if request.url.path == '/models':
            return httpx.Response(200, json={'data': [{'id': 'remote-model'}]})
        return httpx.Response(200, json={'value': 'remote-model' if selected else 'previous'})

    factory = ClientFactory(lambda: registry, transport=HttpxTransport.for_tests(HandlerTransport(handler)))
    capability = factory.admit('remote-box')
    target = dispatch.Target('remote-box', service, offering, doc)
    runtime = dispatch.Runtime(target, assets=MemoryAssetStore(), remote_factory=factory, remote_capability=capability)
    return registry, doc, factory, capability, runtime, requests


async def exercise(path, monkeypatch):
    from gateway.engine_protocol import inventory
    from gateway.engine_protocol.types import JobRef, JobState, JobStatus
    from gateway.swap_drain import SwapDrain
    from gateway.engine_protocol.errors import ProtocolError
    import pytest

    registry, doc, factory, cap, runtime, requests = case()
    assert cap.service_id == 'remote-box' and cap.exact_origin == 'https://engine.example:443'
    assert cap.policy_generation
    with pytest.raises(ProtocolError):
        dispatch.Runtime(runtime.target)
    assert not requests
    if path == 'main':
        await runtime.adapter.submit(runtime.connection_for('smell.infer'), 'smell.infer',
                                     runtime.request('smell.infer', {'vector': [1]}))
    elif path == 'polling':
        await runtime.adapter.poll(runtime.connection_for('smell.infer'), JobRef('smell.infer', 'j'))
    elif path == 'assets':
        ref = JobRef('smell.infer', 'j')
        await runtime.adapter.fetch_assets(runtime.connection_for('smell.infer'), ref, JobStatus(ref, JobState.succeeded))
    elif path == 'inventory':
        monkeypatch.setattr(description, 'load_registry', lambda: {doc.id: doc})
        monkeypatch.setattr(inventory, '_docs_last_good', None)
        monkeypatch.setattr(inventory, '_docs_load', None)
        inventory._cache.clear()
        assert await inventory.models(registry, remote_factory=factory)
    else:
        service = registry['services']['remote-box']
        offering = service['config']['offerings'][0]
        offering['selection'] = {'mode': 'switch', 'switch': {
            'call': 'select', 'args': {}, 'observe_selected': 'chosen'}}
        service['observations'] = {'chosen': (
            {'http': {'path': '/state', 'select': '/value'}} if path == 'observation' else {'op': 'state'})}
        # Re-admit after the owner changes selection/observation policy.
        cap = factory.admit('remote-box')
        runtime = dispatch.Runtime(runtime.target, remote_factory=factory, remote_capability=cap)
        lease = await runtime.lease(drain=SwapDrain(), lock_for=lambda _: asyncio.Lock())
        lease.release()
        assert any(request.url.path == '/select' for request in requests)
    assert requests
    before = len(requests)
    registry['services']['remote-box']['config']['connection']['remote_dispatch']['enabled'] = False
    with pytest.raises(ProtocolError):
        await runtime.invoke('state', {})
    assert len(requests) == before

"""Real SQLite change detection and bounded worker costs at engine I/O."""
import asyncio
import sqlite3

import httpx
import pytest

from gateway import admin_store, engine_egress
from gateway.engine_protocol.errors import ProtocolError
from tests.support.remote_protocol import case
from tests.support.fake_upstream import fake_upstreams  # noqa: F401
from tests.support.fake_upstream import HandlerTransport
from tests.test_signin_who_can_enter import gateway  # noqa: F401
from tests.test_owner_engine_chain_smell import owner, smell_engine, _install_description, _write_headers  # noqa: F401


@pytest.mark.parametrize('failed_read', [False, True])
@pytest.mark.parametrize('remote', [False, True])
def test_legacy_unchanged_registry_needs_no_worker_and_retains_denials(tmp_path, monkeypatch, failed_read, remote):
    path = tmp_path / 'registry.db'
    admin_store.init_db(path)
    registry, _, _, _, _, _ = case()
    monkeypatch.setattr(engine_egress, '_live', engine_egress._live)
    engine_egress.install(engine_egress.project(registry['services'] if remote else {}),
                          registry_path=path, registry_generation=admin_store.service_registry_generation(path=path))
    reader = engine_egress._live.registry_reader
    connection = reader._connection
    queries, sends = [], []
    connection.set_trace_callback(queries.append)
    if failed_read:
        def fail():
            raise sqlite3.OperationalError('synthetic generation read failure')
        monkeypatch.setattr(reader, 'version', fail)
    def worker(*args, **kwargs):
        pytest.fail('unchanged or unreadable legacy registry submitted a worker')
    monkeypatch.setattr(asyncio, 'to_thread', worker)
    def send(request):
        sends.append(request.url)
        return httpx.Response(200, content=b'local result')
    async def run():
        async with engine_egress.async_client(transport=HandlerTransport(send)) as client:
            if remote:
                with pytest.raises(ProtocolError):
                    await client.get('https://engine.example/health')
            else:
                for _ in range(3):
                    assert (await client.get('http://localhost:59124/health')).content == b'local result'
    try:
        asyncio.run(run())
        assert len(sends) == (0 if remote else 3)
        assert reader._connection is connection
        assert all(query == 'PRAGMA data_version' for query in queries)
    finally:
        connection.set_trace_callback(None)


def _remote_factory(owner, *, wire_bytes=None):
    from gateway.remote_engine import factory_from_store
    from tests.support.remote_protocol import add_remote
    client, _, _, _ = owner
    assert _install_description(client).status_code == 201
    assert add_remote(client).status_code == 201
    assert client.patch('/admin/api/operator/services/smell-box', headers=_write_headers(client),
                        json={'enabled': True}).status_code == 200
    if wire_bytes is not None:
        registry = client.get('/admin/api/operator/service-registry').json()['service_registry']
        registry['services']['smell-box']['config']['connection']['remote_dispatch']['limits'] = {'wire_bytes': wire_bytes}
        assert client.put('/admin/api/operator/service-registry', headers=_write_headers(client), json=registry).status_code == 200
    return factory_from_store()


def _external_generation_commit(path):
    # A separate connection represents a CLI commit without in-process callbacks.
    from gateway.sqlite_startup import connect
    conn = connect(path)
    try:
        conn.execute("UPDATE operator_settings_generations SET operator_settings_generation=operator_settings_generation+1 WHERE setting_key='service_registry'")
        conn.commit()
    finally:
        conn.close()


def test_remote_64_mib_download_bounds_worker_and_snapshot_reads(owner, gateway, monkeypatch, fake_upstreams, record_property):
    from collections import Counter
    from gateway.engine_protocol.transport import WireRequest
    from gateway.engine_protocol.types import Timeouts
    from tests.support.remote_protocol import gateway_case
    size = 64 * 1024 * 1024
    recorder = gateway_case(gateway, monkeypatch, fake_upstreams)
    recorder.body_size, recorder.fragment_size = size, 64 * 1024
    factory = _remote_factory(owner, wire_bytes=size * 2)
    bound = factory.transport(factory.admit('smell-box'))
    queries, workers, snapshots = [], [], Counter()
    factory._registry_version._connection.set_trace_callback(queries.append)
    real_thread = asyncio.to_thread
    async def worker(func, *args, **kwargs):
        workers.append(func.__qualname__)
        return await real_thread(func, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', worker)
    for name in ('service_registry_generation', 'get_service_registry_json_with_generation'):
        original = getattr(admin_store, name)
        def counted(*args, _name=name, _original=original, **kwargs):
            snapshots[_name] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(admin_store, name, counted)
    async def run():
        response = await bound.send(WireRequest('GET', 'https://engine.example/download', {}), Timeouts())
        received, changed = 0, False
        try:
            async for chunk in response.chunks():
                assert chunk == b'x' * len(chunk)
                received += len(chunk)
                if received >= size // 2 and not changed:
                    _external_generation_commit(gateway.path)
                    changed = True
                await asyncio.sleep(0)  # also exercise the shared watcher during the transfer
            assert received == size
        finally:
            await response.aclose()
    try:
        asyncio.run(run())
        assert len(recorder.connections) == 1 and recorder.tls == ['engine.example']
        assert recorder.read_sizes[1:] == [64 * 1024] * 1024
        assert sum(recorder.read_sizes) > size  # headers count against the wire cap too
        assert snapshots == {'service_registry_generation': 1, 'get_service_registry_json_with_generation': 1}
        assert len(workers) <= 6, workers  # setup + one refresh, independent of the 1,024 body reads
        assert workers.count('ClientFactory.check') == 3  # two send checks + one changed-generation check
        assert all(query == 'PRAGMA data_version' for query in queries)
        for name, value in {'download_bytes': size, 'body_reads': len(recorder.read_sizes) - 1,
                            'worker_hops': len(workers), 'snapshot_queries': sum(snapshots.values()),
                            'inline_version_queries': len(queries)}.items():
            record_property(name, value)
    finally:
        factory._registry_version._connection.set_trace_callback(None)


def test_remote_changed_version_refreshes_once_for_concurrent_checks(owner, gateway, monkeypatch):
    factory = _remote_factory(owner)
    cap = factory.admit('smell-box')
    bindings = [factory.transport(cap) for _ in range(8)]
    _external_generation_commit(gateway.path)
    real_thread, workers = asyncio.to_thread, []
    async def worker(func, *args, **kwargs):
        workers.append(func)
        return await real_thread(func, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', worker)
    async def run():
        await asyncio.gather(*(bound.check_current() for bound in bindings))
        await asyncio.gather(*(bound.check_current() for bound in bindings))
    asyncio.run(run())
    assert workers == [factory.check]
    assert not any(bound.revoked for bound in bindings)


@pytest.mark.parametrize('io', ['read', 'write'])
def test_remote_unreadable_version_refuses_next_socket_io(owner, gateway, monkeypatch, io):
    from gateway.remote_engine_budget import GuardedStream
    factory = _remote_factory(owner)
    bound = factory.transport(factory.admit('smell-box'))
    calls = []
    class Stream:
        async def read(self, *args, **kwargs):
            calls.append('read')
            return b''
        async def write(self, *args, **kwargs):
            calls.append('write')
    def fail():
        raise sqlite3.OperationalError('synthetic version read failure')
    monkeypatch.setattr(factory._registry_version, 'version', fail)
    async def run():
        stream = GuardedStream(Stream(), bound)
        with pytest.raises(asyncio.CancelledError):
            await (stream.read(1024) if io == 'read' else stream.write(b'synthetic'))
    asyncio.run(run())
    assert bound.revoked and not calls

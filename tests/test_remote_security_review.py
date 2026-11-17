"""Remote authority regressions from the independent security review."""
import asyncio

import pytest

from gateway.engine_protocol.errors import ProtocolError
from tests.support.remote_protocol import SocketRecorder, case


def test_remote_legacy_switch_refuses_before_client_or_credential(monkeypatch, caplog):
    from gateway.engine_control import EngineControlRuntime
    from gateway.swap_drain import SwapDrain
    from gateway import engine_control

    registry, _, _, _, _, _ = case()
    service = registry['services']['remote-box']
    service['protocol_operations'] = {'select': {'method': 'POST', 'path': '/select'}}
    selection = {'mode': 'switch', 'switch': {'call': 'select', 'args': {}, 'observe_selected': 'chosen'}}
    service['config']['offerings'][0]['selection'] = selection
    service['offerings'] = service['config']['offerings']
    recorder = SocketRecorder(monkeypatch)
    attempts = []
    def unbound(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('unbound client or credential lookup')
    monkeypatch.setattr(engine_control, 'credential_headers', unbound)
    runtime = EngineControlRuntime(service, drain=SwapDrain(), lock_for=lambda _: asyncio.Lock(), client_factory=unbound)
    async def check():
        for call in (lambda: runtime.invoke('select', {}), lambda: runtime.select('remote-model', selection), runtime.load):
            with pytest.raises(ProtocolError) as error:
                await call()
            assert error.value.code == 'protocol_dispatch_refused'
    asyncio.run(check())
    assert not attempts and not recorder.connections and not recorder.writes
    assert 'synthetic-control-review' not in caplog.text


@pytest.mark.parametrize('proxy_env', [False, True])
def test_remote_legacy_health_refuses_before_client_or_credential(monkeypatch, proxy_env):
    from gateway import lifecycle_drivers as drivers
    from gateway.engine_observations import ObservationReader
    registry, _, _, _, _, _ = case()
    service = registry['services']['remote-box']
    service['health_endpoint'] = '/health'
    recorder = SocketRecorder(monkeypatch)
    if proxy_env:
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            monkeypatch.setenv(name, 'http://proxy.invalid:8888')
    def unbound(*args, **kwargs):
        raise AssertionError('unbound health client or credential lookup')
    monkeypatch.setattr(drivers.engine_egress, 'async_client', unbound)
    monkeypatch.setattr(drivers, '_health_request', unbound)
    async def check():
        with pytest.raises(ProtocolError):
            await drivers._poll_health(service, .1)
        with pytest.raises(ProtocolError):
            await drivers.ExternalDriver().ensure_ready(service, .1)
        with pytest.raises(ProtocolError):
            await ObservationReader(client_factory=unbound).read(service, 'process_ready')
        with pytest.raises(ProtocolError):
            drivers.driver_for_service('remote-box', {'remote-box': service})
    asyncio.run(check())
    assert not recorder.connections and not recorder.writes


@pytest.mark.parametrize('marked', [True, False])
@pytest.mark.parametrize('adapter_kind', ['chunked_stt', 'inline_tts', 'node_graph'])
def test_remote_unbound_adapters_refused(monkeypatch, adapter_kind, marked):
    from gateway import engine_egress
    from gateway.engine_protocol.adapter import connection_from_record
    from gateway.engine_protocol.types import CanonicalRequest
    from gateway.engine_protocol.chunked_stt import ChunkedSttAdapter
    from gateway.engine_protocol.inline_tts import InlineTtsAdapter
    from gateway.engine_protocol.node_graph import NodeGraphAdapter
    registry, _, _, _, _, _ = case()
    connection = registry['services']['remote-box']['config']['connection']
    conn = connection_from_record(connection, lambda _: 'synthetic-adapter-review')
    monkeypatch.setattr(engine_egress, '_live', engine_egress.project(registry['services']))
    if not marked:
        from dataclasses import replace
        conn = replace(conn, remote_enabled=False)
    recorder = SocketRecorder(monkeypatch)
    def unbound(*args, **kwargs):
        raise AssertionError('unbound adapter client')
    monkeypatch.setattr(engine_egress, 'async_client', unbound)
    adapters = {'chunked_stt': (ChunkedSttAdapter(), 'stt.transcribe'),
                'inline_tts': (InlineTtsAdapter(), 'speech.synthesize'),
                'node_graph': (NodeGraphAdapter(), 'graph.stats')}
    adapter, operation = adapters[adapter_kind]
    async def check():
        with pytest.raises(ProtocolError):
            await adapter.invoke(conn, operation, CanonicalRequest(operation))
        if adapter_kind == 'node_graph':
            with pytest.raises(ProtocolError):
                adapter.attach(conn, object())
            with pytest.raises(ProtocolError):
                await anext(adapter.watch(conn))
    asyncio.run(check())
    assert not recorder.connections and not recorder.writes


def test_remote_unbound_egress_hook_refuses_probe_and_inference(monkeypatch):
    import httpx
    from gateway import engine_egress
    registry, _, _, _, _, _ = case()
    monkeypatch.setattr(engine_egress, '_live', engine_egress.project(registry['services']))
    recorder = SocketRecorder(monkeypatch)
    async def check():
        async with engine_egress.async_client() as client:
            for path in ('/health', '/state', '/v1/infer'):
                with pytest.raises(ProtocolError):
                    await client.get('https://engine.example' + path, extensions=engine_egress.probe())
        with engine_egress.client() as client:
            with pytest.raises(ProtocolError):
                client.get('https://engine.example/health')
    asyncio.run(check())
    assert not recorder.connections and not recorder.writes


def test_remote_blackholed_connect_does_not_block_admin_write(monkeypatch, tmp_path):
    import sqlite3
    import httpcore
    from tests.support.remote_protocol import socket_case
    async def check():
        registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
        database = tmp_path / 'admin.db'
        with sqlite3.connect(database) as conn:
            conn.execute('CREATE TABLE admin_write (value INTEGER)')
        factory.db_path = database
        entered, release = asyncio.Event(), asyncio.Event()
        original = httpcore.AnyIOBackend.connect_tcp
        async def blackhole(backend, *args, **kwargs):
            entered.set()
            await release.wait()
            return await original(backend, *args, **kwargs)
        monkeypatch.setattr(httpcore.AnyIOBackend, 'connect_tcp', blackhole)
        task = asyncio.create_task(runtime.invoke('state', {}))
        await entered.wait()
        def admin_write():
            with sqlite3.connect(database, timeout=.1) as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('INSERT INTO admin_write VALUES (1)')
        async with asyncio.timeout(1):
            await asyncio.to_thread(admin_write)
        assert not task.done()
        registry['services']['remote-box']['config']['connection']['remote_dispatch']['enabled'] = False
        release.set()
        with pytest.raises((asyncio.CancelledError, ProtocolError)):
            await task
        assert not recorder.writes and recorder.closed
    asyncio.run(check())


def test_remote_generation_change_stops_next_socket_io(monkeypatch):
    from gateway.remote_engine_budget import GuardedStream
    from tests.support.remote_protocol import socket_case
    async def check():
        registry, _, _, runtime, _, _ = socket_case(monkeypatch)
        calls = []
        class Stream:
            async def read(self, *args, **kwargs):
                calls.append('read')
                registry['services']['remote-box']['config']['connection']['remote_dispatch']['enabled'] = False
                return b'HTTP/1.1 200 OK\r\n'
            async def write(self, *args, **kwargs):
                calls.append('write')
        stream = GuardedStream(Stream(), runtime._transport)
        with pytest.raises(asyncio.CancelledError):
            await stream.read(1024)
        with pytest.raises(asyncio.CancelledError):
            await stream.write(b'synthetic-must-not-send')
        assert calls == ['read']
    asyncio.run(check())

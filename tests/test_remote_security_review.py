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



def test_remote_authority_cache_runs_off_loop_and_ignores_unrelated_revision(monkeypatch):
    import threading
    from gateway import remote_engine
    registry, _, _, _, _, _ = case()
    snapshot = {'value': registry, 'operator_settings_generation': 1}
    factory = remote_engine.ClientFactory(lambda: snapshot)
    cap = factory.admit('remote-box')
    bound = factory.transport(cap)
    threads, policy_calls = [], []
    original_registry, original_policy = factory._registry, remote_engine.policy
    def read():
        threads.append(threading.get_ident())
        return original_registry()
    def policy(record, **kwargs):
        policy_calls.append(True)
        return original_policy(record, **kwargs)
    factory._registry = read
    monkeypatch.setattr(remote_engine, 'policy', policy)
    async def check():
        loop_thread = threading.get_ident()
        async with bound.operation():
            for _ in range(4):
                await bound.check_current()
            snapshot['operator_settings_generation'] += 1
            registry['services']['remote-box']['description'] = 'unrelated display metadata'
            await bound.check_current()
            assert not bound.revoked
            assert all(thread != loop_thread for thread in threads)
        assert not policy_calls  # the authority digest, limits and validation are cached
    asyncio.run(check())


@pytest.mark.parametrize('writer', ['transaction', 'plain', 'audit', 'with_setting', 'ordinary_in'])
@pytest.mark.parametrize('roles', [None, frozenset({'admin'})])
def test_remote_registry_writers_require_owner_actor(tmp_path, writer, roles):
    from types import SimpleNamespace
    from gateway import admin_store
    database = tmp_path / 'registry.db'
    admin_store.init_db(database)
    registry, _, _, _, _, _ = case()
    actor = None if roles is None else SimpleNamespace(roles=roles)
    common = {'updated_by': 'owner', 'expected_generation': 0, 'actor': actor}
    expected = ValueError if writer == 'ordinary_in' else PermissionError
    message = 'registry transaction primitive' if writer == 'ordinary_in' else 'Only the owner'
    with pytest.raises(expected, match=message):
        if writer == 'ordinary_in':
            with admin_store._conn(database) as conn:
                conn.execute('BEGIN IMMEDIATE')
                admin_store.put_operator_setting_in(conn, 'service_registry', registry, updated_by='owner')
        elif writer == 'transaction':
            with admin_store._conn(database) as conn:
                conn.execute('BEGIN IMMEDIATE')
                admin_store.write_service_registry_in_transaction(conn, registry, now=1, **common)
        elif writer == 'plain':
            admin_store.write_service_registry(registry, path=database, **common)
        elif writer == 'audit':
            admin_store.write_service_registry_with_audit(registry, path=database, audit_action='test',
                audit_actor_kind='user', audit_actor_id='owner', audit_actor_label=None,
                audit_target_kind='test', audit_target_id='test', **common)
        else:
            admin_store.write_service_registry_and_setting_with_audits(registry, path=database,
                setting_key='test', setting_value={}, audits=[], **common)
    assert admin_store.get_service_registry_with_generation(path=database) is None



@pytest.mark.parametrize('answer', ['64:ff9b::7f00:1', '64:ff9b::a9fe:a9fe',
                                  '64:ff9b::a00:1', '::ffff:0:7f00:1', '::127.0.0.1'])
def test_remote_embedded_ipv4_addresses_checked_before_dial(monkeypatch, answer):
    from tests.support.remote_protocol import socket_case
    async def check():
        _, _, _, runtime, recorder, _ = socket_case(monkeypatch, answers=[answer])
        with pytest.raises(ProtocolError):
            await runtime.invoke('state', {})
        assert not recorder.connections and not recorder.writes
    asyncio.run(check())


@pytest.mark.parametrize('trailers', [False, True])
def test_remote_chunked_responses_are_explicitly_unsupported(monkeypatch, trailers):
    from tests.support.remote_protocol import socket_case
    async def check():
        _, _, _, runtime, recorder, _ = socket_case(monkeypatch)
        body = b'{"value":"remote-model"}'
        ending = b'0\r\nX-Result: synthetic-trailer\r\n\r\n' if trailers else b'0\r\n\r\n'
        recorder.responses.append(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n' +
                                  format(len(body), 'x').encode() + b'\r\n' + body + b'\r\n' + ending)
        with pytest.raises(ProtocolError) as error:
            await runtime.invoke('state', {})
        assert error.value.code == 'protocol_dispatch_refused'
        assert recorder.closed
    asyncio.run(check())



@pytest.mark.parametrize('port', [443, 5000, 15000, 17777])
def test_remote_private_cidr_never_authorizes_gateway_host_ports(monkeypatch, port):
    import ipaddress
    from gateway import engine_check
    from gateway.engine_protocol import dispatch
    from tests.support.remote_protocol import socket_case
    async def check():
        private_range = '10.0.0.0/24'
        peer = str(ipaddress.ip_network(private_range).network_address + 1)
        registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch, answers=[peer])
        connection = registry['services']['remote-box']['config']['connection']
        connection['address'] = f'https://engine.example:{port}'
        connection['remote_dispatch']['private_cidrs'] = [private_range]
        cap = factory.admit('remote-box')  # the valid CIDR can contain an address of this host
        runtime = dispatch.Runtime(runtime.target, remote_factory=factory, remote_capability=cap)
        monkeypatch.setattr(engine_check, 'is_this_computer', lambda address: True)
        with pytest.raises(ProtocolError):
            await runtime.invoke('state', {})
        assert not recorder.connections and not recorder.writes
        # The same exception still permits another host, on the same port.
        monkeypatch.setattr(engine_check, 'is_this_computer', lambda address: False)
        await runtime.invoke('state', {})
        assert recorder.connections == [(peer, port)]
    asyncio.run(check())


@pytest.mark.parametrize('caller', ['adapter', 'listener'])
def test_remote_graph_websocket_guard_precedes_actual_dial(monkeypatch, caller):
    import proxy
    from gateway import engine_egress, remote_engine
    from gateway.engine_protocol.node_graph import NodeGraphAdapter
    from gateway.engine_protocol.types import Connection
    import websockets.asyncio.client
    registry, _, _, _, _, _ = case()
    monkeypatch.setattr(engine_egress, '_live', engine_egress.project(registry['services']))
    # Legacy listeners construct an unmarked Connection from the published address.
    connection = Connection(address='https://engine.example')
    adapter = NodeGraphAdapter()
    recorder, dials = SocketRecorder(monkeypatch), []
    def dial(*args, **kwargs):
        dials.append(True)
        raise AssertionError('remote websocket dial')
    monkeypatch.setattr(websockets.asyncio.client, 'connect', dial)
    async def check():
        if caller == 'adapter':
            with pytest.raises(ProtocolError):
                await anext(adapter.watch(connection))
        else:
            monkeypatch.setattr(proxy, '_graph_adapter', lambda *_: adapter)
            monkeypatch.setattr(proxy, '_graph_connection', lambda **_: connection)
            stop, checked = asyncio.Event(), asyncio.Event()
            original = remote_engine.refuse_unbound_connection
            def guard(conn):
                try:
                    original(conn)
                finally:
                    checked.set()
            monkeypatch.setattr(remote_engine, 'refuse_unbound_connection', guard)
            task = asyncio.create_task(proxy._graph_ws_listener(stop))
            async with asyncio.timeout(1):
                await checked.wait()
                stop.set()
                await task
    asyncio.run(check())
    assert not dials and not recorder.connections and not recorder.writes

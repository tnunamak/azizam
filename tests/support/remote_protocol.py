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
        'cost_class': 'free-local',
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


class SocketRecorder:
    """Replace only the OS socket boundary; keep httpx/httpcore framing and TLS setup."""
    def __init__(self, monkeypatch):
        import httpcore
        self.connections, self.writes, self.tls, self.closed = [], [], [], []
        self.posts = []
        self.paths = []
        self.responses = []
        recorder = self

        class Stream:
            def __init__(self):
                self.response = b''
                self.answered = False
            async def write(self, buffer, timeout=None):
                if getattr(recorder, 'on_write', None) is not None:
                    recorder.on_write()
                recorder.writes.append(bytes(buffer))
                if buffer.startswith((b'GET ', b'POST ')):
                    line = buffer.split(b'\r\n', 1)[0]
                    path = line.split(b' ')[1].decode()
                    recorder.paths.append(path)
                    self.path = path
                    if recorder.responses:
                        self.response = recorder.responses.pop(0)
                    else:
                        payload = (b'{"job":{"id":"j"}}' if path == '/v1/infer' else
                                   (b'{"state":"working"}' if getattr(recorder, 'pending', False) else b'{"state":"ready"}') if path == '/v1/infer/j' else
                                   b'remote-result' if path.endswith('/molecule') else b'{"value":"remote-model"}')
                        if path == '/state' and getattr(recorder, 'identity', None):
                            payload = json.dumps({'value': recorder.identity}).encode()
                        ctype = b'application/octet-stream' if path.endswith('/molecule') else b'application/json'
                        self.response = (b'HTTP/1.1 200 OK\r\nContent-Type: ' + ctype + b'\r\nContent-Length: ' +
                                         str(len(payload)).encode() + b'\r\nConnection: close\r\n\r\n' + payload)
                elif buffer and getattr(self, 'path', None) == '/v1/infer':
                    recorder.posts.append(json.loads(buffer))
            async def read(self, max_bytes, timeout=None):
                if getattr(recorder, 'block_read', None) is not None:
                    await recorder.block_read.wait()
                if getattr(recorder, 'read_delay', None):
                    await asyncio.sleep(recorder.read_delay)
                max_bytes = min(max_bytes, getattr(recorder, 'fragment_size', max_bytes))
                data, self.response = self.response[:max_bytes], self.response[max_bytes:]
                return data
            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                import ssl
                assert ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname
                recorder.tls.append(server_hostname)
                return self
            def get_extra_info(self, info):
                return None
            async def aclose(self):
                recorder.closed.append(self)

        async def connect_tcp(backend, host, port, **kwargs):
            if getattr(recorder, 'on_connect', None) is not None:
                recorder.on_connect(host, port)
            recorder.connections.append((host, port))
            return Stream()
        monkeypatch.setattr(httpcore.AnyIOBackend, 'connect_tcp', connect_tcp)


def socket_case(monkeypatch, answers=None):
    from gateway.remote_engine import ClientFactory
    from gateway.remote_engine_transport import RemoteNetworkTransport
    registry, doc, _, _, runtime, _ = case()
    recorder = SocketRecorder(monkeypatch)
    answers = answers if answers is not None else ['93.184.216.34']
    async def resolve(host, port):
        return list(answers)
    factory = ClientFactory(lambda: registry, connector=lambda bound: RemoteNetworkTransport(bound, resolve=resolve))
    cap = factory.admit('remote-box')
    runtime = dispatch.Runtime(runtime.target, assets=MemoryAssetStore(), remote_factory=factory, remote_capability=cap)
    return registry, factory, cap, runtime, recorder, answers


async def canary_path(path, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    from gateway.engine_protocol.types import CanonicalRequest, JobRef, JobState, JobStatus
    registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
    inbound = 'synthetic-inbound-' + path
    service_value = 'synthetic-service-' + path
    monkeypatch.setenv('REMOTE_TEST_CREDENTIAL', service_value)
    registry['services']['remote-box']['config']['connection']['credentials'] = {
        cls: {'secret_ref': 'os.environ/REMOTE_TEST_CREDENTIAL', 'origin': 'https://engine.example',
              'header': 'Authorization', 'scheme': 'Bearer'} for cls in ('inference', 'control')}
    cap = factory.admit('remote-box')
    runtime = dispatch.Runtime(runtime.target, assets=MemoryAssetStore(), remote_factory=factory, remote_capability=cap)
    request = CanonicalRequest('state', headers={'Authorization': inbound, 'Cookie': inbound, 'X-API-Key': inbound})
    if path == 'observation':
        await fresh_observation(runtime, factory)
    elif path == 'poll':
        await runtime.adapter.poll(runtime.connection_for('smell.infer'), JobRef('smell.infer', 'j'))
    elif path == 'asset':
        ref = JobRef('smell.infer', 'j')
        await runtime.adapter.fetch_assets(runtime.connection_for('smell.infer'), ref, JobStatus(ref, JobState.succeeded))
    else:
        await runtime.adapter.invoke(runtime.connection_for('state', probe=path == 'probe'), 'state', request)
    captured = b''.join(recorder.writes).decode()
    assert inbound not in captured and inbound not in caplog.text
    assert service_value not in caplog.text
    assert 'Authorization: Bearer ' + service_value in captured or 'authorization: Bearer ' + service_value in captured
    assert recorder.connections == [('93.184.216.34', 443)]
    assert recorder.tls == ['engine.example']


async def fresh_observation(runtime, factory):
    from gateway.swap_drain import SwapDrain
    service = runtime.target.service
    service['config']['offerings'][0]['selection'] = {'mode': 'switch', 'switch': {
        'call': 'select', 'args': {}, 'observe_selected': 'chosen'}}
    service['observations'] = {'chosen': {'http': {'path': '/state', 'select': '/value'}}}
    cap = factory.admit(runtime.target.engine_id)
    runtime = dispatch.Runtime(runtime.target, remote_factory=factory, remote_capability=cap)
    lease = await runtime.lease(drain=SwapDrain(), lock_for=lambda _: asyncio.Lock())
    lease.release()


async def real_tls_identity(monkeypatch, directory):
    """Run the connector and TLS library against a synthetic local TLS server."""
    import datetime
    import ssl
    import httpcore
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from gateway.engine_protocol.errors import ProtocolError
    import pytest

    real_connect = httpcore.AnyIOBackend.connect_tcp
    original_context = ssl.create_default_context
    for identity in ('engine.example', 'wrong.example'):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity)])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                       .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                       .not_valid_after(now + datetime.timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName(identity)]), critical=False)
                       .sign(key, hashes.SHA256()))
        cert_path, key_path = directory / 'synthetic-cert.pem', directory / 'synthetic-key.pem'
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        received = []
        async def serve(reader, writer):
            try:
                data = await reader.readuntil(b'\r\n\r\n')
                received.append(data)
                payload = b'{"value":"remote-model"}'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: ' +
                             str(len(payload)).encode() + b'\r\n\r\n' + payload)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0, ssl=server_context)
        port = server.sockets[0].getsockname()[1]
        dialled = []
        async def redirect_socket(backend, host, requested_port, **kwargs):
            dialled.append((host, requested_port))
            assert host == '93.184.216.34' and requested_port == 443
            return await real_connect(backend, '127.0.0.1', port, **kwargs)
        def context(*args, **kwargs):
            result = original_context(*args, **kwargs)
            result.load_verify_locations(cafile=str(cert_path))
            return result
        try:
            _, _, _, runtime, _, _ = socket_case(monkeypatch)
            monkeypatch.setattr(httpcore.AnyIOBackend, 'connect_tcp', redirect_socket)
            monkeypatch.setattr(ssl, 'create_default_context', context)
            if identity == 'engine.example':
                await runtime.invoke('state', {})
                assert received and b'Host: engine.example' in received[0]
            else:
                with pytest.raises(ProtocolError):
                    await runtime.invoke('state', {})
                assert received == []
            assert dialled == [('93.184.216.34', 443)]
        finally:
            server.close()
            await server.wait_closed()
            monkeypatch.setattr(httpcore.AnyIOBackend, 'connect_tcp', real_connect)
            monkeypatch.setattr(ssl, 'create_default_context', original_context)
            key_path.unlink()
            cert_path.unlink()


def gateway_case(gateway, monkeypatch, fake_upstreams):
    from gateway import engine_check, remote_engine
    if not remote_engine.PRODUCTION_READY:
        monkeypatch.setattr(remote_engine, 'PRODUCTION_READY', True)
    recorder = SocketRecorder(monkeypatch)
    recorder.identity = 'smell-1'
    from tests.test_owner_engine_chain_smell import EmptyModelCatalog
    if '127.0.0.1:4011' not in fake_upstreams.router._routes:
        fake_upstreams.router.add('127.0.0.1:4011', EmptyModelCatalog())
    original = engine_check._default_resolve
    async def resolve(host, port=None):
        if host == 'engine.example':
            return ['93.184.216.34']
        return await original(host, port)
    monkeypatch.setattr(engine_check, '_default_resolve', resolve)
    return recorder


def add_remote(client, *, observations=False):
    from tests.test_owner_engine_chain_smell import _write_headers
    offering = {'id': 'smell-1', 'upstream': 'smell-1', 'kind': 'model',
                'tasks': [{'task': 'smell.infer'}], 'selection': {'mode': 'request_field'}}
    extra = {}
    if observations:
        offering['selection'] = {'mode': 'switch', 'switch': {
            'call': 'select', 'args': {}, 'observe_selected': 'chosen'}}
        extra['observations'] = {'chosen': {'http': {'path': '/state', 'select': '/value'}}}
    created = client.post('/admin/api/operator/services', headers=_write_headers(client), json={
        'name': 'smell-box', 'base_url': 'https://engine.example', 'protocol': 'd0-smell',
        'cost_class': 'free-local',
        'enabled': False, 'capabilities': ['smell.infer'], 'offerings': [offering],
        'connection': {'dispatch': 'protocol', 'protocol': 'd0-smell', 'address': 'https://engine.example',
                       'remote_dispatch': {'enabled': True, 'version': 1}}})
    if observations and created.status_code == 201:
        # The canonical observation block is on the service record, not legacy config.
        registry = client.get('/admin/api/operator/service-registry').json()['service_registry']
        registry['services']['smell-box']['observations'] = extra['observations']
        stored = client.put('/admin/api/operator/service-registry', headers=_write_headers(client), json=registry)
        assert stored.status_code == 200, stored.text
    return created

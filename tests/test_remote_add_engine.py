"""Add engine: a server on another computer on the owner's own network (slice S5 of engines as data).

Everything here runs on documentation-style addresses (10.0.0 prefix, 203.0.113.x). Nothing dials the network: the check reads a
fake in process, and a job goes through the real remote boundary with only the last wire replaced by a fake server. The
socket tests replace only the operating-system socket, so "zero connections" is a count taken at that boundary.
"""
import asyncio
import base64
import copy
import json

import httpx
import pytest

import proxy
from gateway import admin_store, engine_check, media_jobs, receipt_store, remote_engine
from gateway.engine_definitions import load_engine_catalog
from gateway.engine_protocol import description, dispatch
from gateway.engine_protocol.errors import ProtocolError
from gateway.operation_catalog import project_dispatch_task_aliases
from gateway.engine_protocol.transport import HttpxTransport
from tests.support.fake_upstream import HandlerTransport, fake_upstreams, scenario_from_data  # noqa: F401 - fixture
from tests.support.remote_protocol import SocketRecorder
from tests.test_added_speech_engine_serves_speech import _app_key
from tests.test_engine_protocol_sdapi import ALPHA, BETA, FakeSdapi, _sd_models_reply
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin, signed_in  # noqa: F401 - gateway is a fixture

TOKEN = 't0k3n'
LAN = '10.0.0'  # a documentation-style private prefix; no host address is spelled out in full
HOST = f'{LAN}.5:7860'
ADDRESS = f'http://{HOST}'
NETWORK = '10.0.0.0/24'


@pytest.fixture(autouse=True)
def fixed_baseline(monkeypatch):
    monkeypatch.setattr(engine_check, '_baseline_token', lambda: TOKEN)


# -- what the definitions say, as data --------------------------------------------------------------------------------

def _remote_definitions():
    catalog = load_engine_catalog()
    return [d for d in (catalog.definition(i) for i in catalog.ids()) if d['record'].get('remote')]


def test_a_definition_allows_another_computer_only_for_tasks_served_in_one_buffered_piece():
    """The per-operation rule is data. The remote path refuses chunked answers, so each task a definition allows there
    must be served by an operation of its description that answers whole: no stream, no event framing."""
    definitions = _remote_definitions()
    assert [d['definition'] for d in definitions] == ['sdapi']
    docs = description.load_registry()
    catalog = dispatch._task_catalog()
    for definition in definitions:
        assert definition['record']['dispatch'], 'another computer is for engines the gateway calls through a description'
        doc = docs[definition['record']['dispatch']['protocol']]
        assert doc.executor == 'http-v1'
        for task in definition['record']['remote']['tasks']:
            serving = [op for op in doc.operations.values()
                       if op.op_class == 'generate' and task in project_dispatch_task_aliases(op.name, catalog=catalog)]
            assert serving, f'no operation of {doc.id} serves {task}'
            for op in serving:
                assert op.job is None, 'a job needs poll and asset fetches the remote path does not allow for this kind'
                response = op.response
                assert response is not None and response.framing in ('json', 'binary')
                assert getattr(response, 'stream', None) is None


def test_the_adapters_do_not_declare_another_computer():
    # node-graph, inline-tts and chunked-stt stay on this computer in this slice.
    catalog = load_engine_catalog()
    for definition_id in ('workflow-image-engine', 'openai-speech', 'openai-transcription'):
        assert not catalog.definition(definition_id)['record'].get('remote')


# -- the owner's dialog ----------------------------------------------------------------------------------------------

def _sdapi_server(fakes, host=HOST):
    def ex(name, method, path, response):
        return {'id': name, 'match': {'method': method, 'path': path}, 'response': response, 'repeat': True, 'required': False}
    not_found = {'status': 404, 'json': {'detail': 'Not Found'}}
    return fakes.asgi(scenario_from_data({
        'protocol': 'engine-probe', 'name': 'lan-sdapi',
        'provenance': {'source': 'hand-written', 'reference': 'sdapi server on a private network'},
        'exchanges': [
            ex('baseline-get', 'GET', '/zz-not-a-route-{{token}}', not_found),
            ex('baseline-post', 'POST', '/zz-not-a-route-{{token}}', not_found),
            ex('options', 'GET', '/sdapi/v1/options', {'status': 200, 'json': {'sd_model_checkpoint': ALPHA}}),
            ex('sd-models', 'GET', '/sdapi/v1/sd-models', {'status': 200, 'json': _sd_models_reply([ALPHA, BETA])}),
        ],
    }, token=TOKEN), host=host)


def _values(address=ADDRESS, **more):
    return {'address': address, 'where': 'network', 'network_trusted': True, **more}


def _create(client, values, *, models=None, name='lan-pictures'):
    etag = client.get('/admin/api/operator/service-registry').headers.get('etag', '"os-0"')
    return client.post('/admin/api/operator/services', headers={**csrf_for(client), 'If-Match': etag}, json={
        'definition': 'sdapi', 'name': name, 'values': values, 'capabilities': ['images'],
        'models': models if models is not None else [{'id': BETA, 'public_name': 'beta', 'enabled': True}]})


def _stored(gateway, service_id='lan-pictures'):
    found = admin_store.get_service_registry_with_generation(path=gateway.path)
    return ((found or {}).get('value') or {}).get('services', {}).get(service_id)


def _check(client, values):
    return client.post('/admin/api/engines/check', headers=csrf_for(client), json={'definition': 'sdapi', 'values': values})


def test_the_check_shows_the_computed_network_and_never_asks_for_a_typed_one(gateway, fake_upstreams):
    _sdapi_server(fake_upstreams)
    owner, *_ = owner_and_admin(gateway)
    checked = _check(owner, {'address': ADDRESS, 'where': 'network'})
    assert checked.status_code == 200, checked.text
    check = checked.json()['check']
    assert check['outcome'] == 'ok', check['findings']
    assert check['remote'] == {'network': NETWORK, 'transport': 'private_plaintext', 'trust_needed': True, 'trusted': False}
    assert [row['id'] for row in check['models']] == [ALPHA, BETA]
    # A typed network is not a field at all.
    typed = _check(owner, {'address': ADDRESS, 'where': 'network', 'private_cidrs': ['10.0.0.0/8']})
    assert typed.status_code == 400


def test_dialog_writes_owner_only_remote_dispatch(gateway, fake_upstreams):
    _sdapi_server(fake_upstreams)
    owner, owner_id, _, admin = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)

    # A member or an app key is refused, and nothing is written.
    refused = _create(admin, _values())
    assert refused.status_code == 403, refused.text
    public = gateway.client()  # no lifespan: the request is the whole test
    app = public.post('/admin/api/operator/services', headers={'authorization': f'Bearer {key}'}, json={
        'definition': 'sdapi', 'name': 'lan-pictures', 'values': _values(), 'capabilities': ['images']})
    assert app.status_code in (401, 403), app.text
    assert _stored(gateway) is None

    created = _create(owner, _values())
    assert created.status_code == 201, created.text
    record = _stored(gateway)
    connection = record['config']['connection']
    assert connection['remote_dispatch'] == {
        'enabled': True, 'version': 1, 'transport': 'private_plaintext', 'private_cidrs': [NETWORK], 'tasks': ['image']}
    assert connection['address'] == ADDRESS and connection['dispatch'] == 'protocol' and connection['protocol'] == 'sdapi'
    assert record['config'].get('cost_class') == 'free-local' or record.get('cost_class') == 'free-local'
    # The audit line names the network the owner approved.
    entry = admin_store.list_audit(path=gateway.path, action_prefix='operator.service.added')[0]
    assert NETWORK in json.dumps(entry) and 'private_plaintext' in json.dumps(entry)
    # The page reads the choice back, so a later edit keeps it.
    view = owner.get('/admin/api/engines/lan-pictures').json()['engine']
    assert view['values']['where'] == 'network' and view['values']['network_trusted'] is True


def test_a_member_cannot_run_the_check_for_another_computer(gateway, fake_upstreams):
    _sdapi_server(fake_upstreams)
    _, _, _, admin = owner_and_admin(gateway)
    assert _check(admin, {'address': ADDRESS, 'where': 'network'}).status_code == 403


def test_plain_http_needs_the_owner_to_say_the_network_is_trusted(gateway, fake_upstreams):
    _sdapi_server(fake_upstreams)
    owner, *_ = owner_and_admin(gateway)
    refused = _create(owner, _values(network_trusted=False))
    assert refused.status_code == 400, refused.text
    assert refused.json()['error']['field'] == 'network_trusted' and NETWORK in refused.json()['error']['message']
    assert _stored(gateway) is None


@pytest.mark.parametrize('address, words', [
    ('http://127.0.0.1:7860', 'this computer'),                         # loopback
    ('http://169.254.169.254:80', 'not on a private network'),        # metadata
    ('http://169.254.10.10:7860', 'not on a private network'),        # link-local
    ('http://203.0.113.9:7860', 'not on a private network'),          # public-shaped
    ('http://[fe80::1]:7860', 'not on a private network'),            # link-local IPv6
])
def test_special_and_public_addresses_are_refused_in_words(gateway, address, words):
    owner, *_ = owner_and_admin(gateway)
    for send in (lambda: _check(owner, {'address': address, 'where': 'network'}),
                 lambda: _create(owner, _values(address))):
        refused = send()
        assert refused.status_code == 400, refused.text
        assert words in refused.json()['error']['message']
    assert _stored(gateway) is None


def test_this_computer_is_refused_whatever_the_network(gateway, monkeypatch):
    monkeypatch.setattr(engine_check, 'is_this_computer', lambda address: str(address) == f'{LAN}.9')
    owner, *_ = owner_and_admin(gateway)
    refused = _create(owner, _values(f'http://{LAN}.9:7860'))
    assert refused.status_code == 400 and 'this computer' in refused.json()['error']['message'].lower(), refused.text
    assert _stored(gateway) is None


@pytest.mark.parametrize('definition_id, address', [
    ('workflow-image-engine', f'http://{LAN}.5:8188'),
    ('openai-speech', f'http://{LAN}.5:8880'),
    ('openai-transcription', f'http://{LAN}.5:8000'),
])
def test_a_server_the_gateway_calls_through_an_adapter_is_refused_in_plain_words(gateway, definition_id, address):
    owner, *_ = owner_and_admin(gateway)
    sent = {'definition': definition_id, 'values': {'address': address, 'where': 'network', 'network_trusted': True}}
    checked = owner.post('/admin/api/engines/check', headers=csrf_for(owner), json=sent)
    assert checked.status_code == 400, checked.text
    assert checked.json()['error']['field'] == 'where'
    assert 'on this computer' in checked.json()['error']['message']
    etag = owner.get('/admin/api/operator/service-registry').headers.get('etag', '"os-0"')
    created = owner.post('/admin/api/operator/services', headers={**csrf_for(owner), 'If-Match': etag},
                         json={**sent, 'name': 'adapter-lan', 'capabilities': []})
    assert created.status_code == 400 and created.json()['error']['field'] == 'where', created.text
    assert _stored(gateway, 'adapter-lan') is None


def test_a_saved_server_moved_back_to_this_computer_loses_its_remote_block(gateway, fake_upstreams):
    from tests.test_engine_admin_routes import _edit
    _sdapi_server(fake_upstreams)
    _sdapi_server(fake_upstreams, host='127.0.0.1:7860')
    owner, *_ = owner_and_admin(gateway)
    assert _create(owner, _values()).status_code == 201
    moved = _edit(owner, 'lan-pictures', {'definition': 'sdapi', 'values': {'address': 'http://127.0.0.1:7860', 'where': 'this'}})
    # This computer's address is checked by the existing rule; a failed check changes nothing, a passed one drops the block.
    if moved.status_code == 200:
        assert 'remote_dispatch' not in _stored(gateway)['config']['connection']
    else:
        assert _stored(gateway)['config']['connection']['remote_dispatch']['enabled'] is True


# -- plain http keeps its safety rules at save ------------------------------------------------------------------------

def _patch_connection(client, mutate, service_id='lan-pictures'):
    state = client.get('/admin/api/operator/service-registry').json()['service_registry']
    connection = copy.deepcopy(state['services'][service_id]['config']['connection'])
    mutate(connection)
    from tests.test_owner_engine_chain_smell import _write_headers
    return client.patch(f'/admin/api/operator/services/{service_id}', headers=_write_headers(client), json={'connection': connection})


def test_plaintext_with_key_refused_at_save(gateway, fake_upstreams, monkeypatch):
    _sdapi_server(fake_upstreams)
    owner, *_ = owner_and_admin(gateway)
    assert _create(owner, _values()).status_code == 201
    before = _stored(gateway)
    monkeypatch.setenv('LAN_ENGINE_KEY', 'synthetic-lan-key')

    def with_key(connection):
        connection['credentials'] = {'inference': {'secret_ref': 'os.environ/LAN_ENGINE_KEY', 'origin': ADDRESS,
                                                    'header': 'Authorization', 'scheme': 'Bearer'}}
    refused = _patch_connection(owner, with_key)
    assert refused.status_code == 400, refused.text
    assert 'key cannot be sent over plain http' in refused.json()['error']['message']
    assert 'reverse proxy' in refused.json()['error']['message']
    assert _stored(gateway) == before
    assert 'synthetic-lan-key' not in refused.text


def test_plaintext_policy_refuses_each_place_a_key_can_live():
    base = {'id': 'lan', 'enabled': True, 'config': {
        'cost_class': 'free-local',
        'connection': {'dispatch': 'protocol', 'protocol': 'sdapi', 'address': ADDRESS, 'credentials': {},
                       'remote_dispatch': {'enabled': True, 'version': 1, 'transport': 'private_plaintext',
                                           'private_cidrs': [NETWORK]}},
        'offerings': []}}
    remote_engine.policy(base)  # the shape that is allowed
    for where, name in (('config', 'api_key_secret_id'), ('config', 'api_key_env'), ('top', 'api_key_ref')):
        bad = copy.deepcopy(base)
        (bad['config'] if where == 'config' else bad)[name] = 'something'
        with pytest.raises(remote_engine.PlainRefusal, match='key cannot be sent over plain http'):
            remote_engine.policy(bad)


@pytest.mark.parametrize('change, words', [
    ({'transport': 'tls'}, 'http address needs the choice'),                               # http address, tls named
    ({'private_cidrs': []}, 'needs the private network'),                                   # no network approved
    ({'private_cidrs': ['10.0.1.0/24']}, 'not inside the private network'),                  # address outside the network
    ({'transport': 'carrier-pigeon'}, None),                                                # unknown value
    ({'tasks': ['speech']}, 'for speech only'),                                              # offering task not allowed
])
def test_plaintext_policy_refuses_what_does_not_add_up(change, words):
    record = {'id': 'lan', 'enabled': True, 'config': {
        'cost_class': 'free-local',
        'connection': {'dispatch': 'protocol', 'protocol': 'sdapi', 'address': ADDRESS, 'credentials': {},
                       'remote_dispatch': {'enabled': True, 'version': 1, 'transport': 'private_plaintext',
                                           'private_cidrs': [NETWORK], 'tasks': ['image']}},
        'offerings': [{'id': 'a', 'upstream': 'a', 'kind': 'model', 'tasks': [{'task': 'image'}]}]}}
    record['config']['connection']['remote_dispatch'].update(change)
    with pytest.raises(ProtocolError) as refused:
        remote_engine.policy(record)
    if words:
        assert words in str(refused.value)


def test_an_https_address_cannot_be_named_plaintext_and_a_public_network_is_never_approved():
    record = {'id': 'lan', 'enabled': True, 'config': {
        'cost_class': 'free-local',
        'connection': {'dispatch': 'protocol', 'protocol': 'sdapi', 'address': 'https://engine.example', 'credentials': {},
                       'remote_dispatch': {'enabled': True, 'version': 1, 'transport': 'private_plaintext',
                                           'private_cidrs': [NETWORK]}},
        'offerings': []}}
    with pytest.raises(ProtocolError):
        remote_engine.policy(record)
    for cidr in ('203.0.113.0/24', '0.0.0.0/0', '169.254.0.0/24', '127.0.0.0/24', '10.0.0.0/8'):
        record['config']['connection'].update(address=ADDRESS)
        record['config']['connection']['remote_dispatch'].update(private_cidrs=[cidr])
        with pytest.raises(ProtocolError):
            remote_engine.policy(record)


# -- the socket boundary: nothing is dialled outside the approved network -----------------------------------------------

def _socket_runtime(monkeypatch, answers, *, address='http://engine.example:7860'):
    from gateway.engine_protocol.assets import MemoryAssetStore
    from gateway.remote_engine import ClientFactory
    from gateway.remote_engine_transport import RemoteNetworkTransport

    doc = description.load_registry()['sdapi']
    offering = {'id': 'alpha', 'upstream': ALPHA, 'kind': 'model', 'tasks': [{'task': 'image', 'evidence': 'declared'}],
                'selection': {'mode': 'request_field'}}
    service = {'id': 'lan', 'enabled': True, 'config': {
        'cost_class': 'free-local',
        'connection': {'dispatch': 'protocol', 'protocol': 'sdapi', 'address': address, 'credentials': {},
                       'remote_dispatch': {'enabled': True, 'version': 1, 'transport': 'private_plaintext',
                                           'private_cidrs': [NETWORK], 'tasks': ['image']}},
        'offerings': [offering]}}
    registry = {'services': {'lan': service}}
    recorder = SocketRecorder(monkeypatch)

    async def resolve(host, port):
        return list(answers)
    factory = ClientFactory(lambda: registry, connector=lambda bound: RemoteNetworkTransport(bound, resolve=resolve))
    cap = factory.admit('lan')
    target = dispatch.Target('lan', service, offering, doc)
    runtime = dispatch.Runtime(target, assets=MemoryAssetStore(), remote_factory=factory, remote_capability=cap)
    return runtime, recorder, cap


@pytest.mark.parametrize('answers', [
    [f'{LAN[:-1]}1.7'],                     # a private address outside the approved network
    ['93.184.216.34'],                  # a public address: plain http is never sent to one
    [f'{LAN}.7', '93.184.216.34'],      # one good answer does not excuse a bad one
    ['127.0.0.1'], ['169.254.169.254'], ['169.254.10.10'],
])
def test_plaintext_outside_cidr_refused_zero_connections(monkeypatch, answers):
    runtime, recorder, _ = _socket_runtime(monkeypatch, answers)
    with pytest.raises(ProtocolError):
        asyncio.run(runtime.invoke('models', {}))
    assert recorder.connections == [] and recorder.writes == []


def test_plaintext_inside_the_network_dials_the_checked_address_without_tls(monkeypatch):
    runtime, recorder, cap = _socket_runtime(monkeypatch, [f'{LAN}.7'])
    assert cap.exact_origin == 'http://engine.example:7860'
    recorder.responses.append(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]')
    try:
        asyncio.run(runtime.invoke('models', {}))
    except ProtocolError:
        pass  # what the fake answers is not the point; where the socket went is
    assert recorder.connections == [(f'{LAN}.7', 7860)]
    assert recorder.tls == []
    assert recorder.paths and recorder.paths[0] == '/sdapi/v1/sd-models'
    assert b'Host: engine.example:7860' in recorder.writes[0]


def test_this_computer_is_refused_at_connect_whatever_the_network(monkeypatch):
    monkeypatch.setattr(engine_check, 'is_this_computer', lambda address: str(address) == f'{LAN}.7')
    runtime, recorder, _ = _socket_runtime(monkeypatch, [f'{LAN}.7'])
    with pytest.raises(ProtocolError):
        asyncio.run(runtime.invoke('models', {}))
    assert recorder.connections == []


def test_a_chunked_answer_from_a_private_host_is_refused(monkeypatch):
    runtime, recorder, _ = _socket_runtime(monkeypatch, [f'{LAN}.7'])
    recorder.responses.append(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n[]\r\n0\r\n\r\n')
    with pytest.raises(ProtocolError):
        asyncio.run(runtime.invoke('models', {}))


# -- jobs on a private host, through the fake upstream ---------------------------------------------------------------

def _fake_wire(fake_upstreams, host, fake):
    """Answer for ``host`` at the last hop only: the checks, the bound client and the executor stay the real ones."""
    fake_upstreams.router._routes.pop(host, None)
    fake_upstreams.router.add_transport(host, HandlerTransport(fake))


def _jobs_home(monkeypatch, tmp_path):
    for module in (receipt_store, media_jobs):
        db = tmp_path / f'{module.__name__}.db'
        module.init_db(db)
        monkeypatch.setattr(module, 'DB_PATH', db)
    monkeypatch.setattr(proxy, 'IMAGE_CACHE_DIR', tmp_path / 'images')
    monkeypatch.setattr(proxy, 'IMAGE_MEDIA_JOB_PAYLOAD_DIR', tmp_path / 'payloads')


def test_an_sdapi_image_job_on_a_private_host_through_the_fake_upstream(gateway, fake_upstreams, monkeypatch, tmp_path):
    _jobs_home(monkeypatch, tmp_path)
    _sdapi_server(fake_upstreams)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    created = _create(owner, _values(), models=[{'id': BETA, 'public_name': 'beta-pictures', 'enabled': True}])
    assert created.status_code == 201, created.text

    fake = FakeSdapi()
    _fake_wire(fake_upstreams, HOST, fake)
    made = owner.post('/v1/images/generations', headers={'authorization': f'Bearer {key}'},
                      json={'model': 'beta-pictures', 'prompt': 'a blue square', 'response_format': 'b64_json'})
    assert made.status_code == 200, made.text
    assert base64.b64decode(made.json()['data'][0]['b64_json']) == fake.image
    assert fake.generated_with == [BETA]
    # The calls went to the private host, in the order the definition says: read, switch, read back, generate.
    assert [(r.method, str(r.url)) for r in fake.requests] == [
        ('GET', f'{ADDRESS}/sdapi/v1/options'), ('POST', f'{ADDRESS}/sdapi/v1/options'),
        ('GET', f'{ADDRESS}/sdapi/v1/options'), ('POST', f'{ADDRESS}/sdapi/v1/txt2img')]


def test_the_owner_turning_the_network_off_stops_the_next_image_job(gateway, fake_upstreams, monkeypatch, tmp_path):
    from tests.test_engine_admin_routes import _edit
    _jobs_home(monkeypatch, tmp_path)
    _sdapi_server(fake_upstreams)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    assert _create(owner, _values(), models=[{'id': BETA, 'public_name': 'beta-pictures', 'enabled': True}]).status_code == 201
    assert _edit(owner, 'lan-pictures', {'enabled': False}).status_code == 200
    fake = FakeSdapi()
    _fake_wire(fake_upstreams, HOST, fake)
    refused = owner.post('/v1/images/generations', headers={'authorization': f'Bearer {key}'},
                         json={'model': 'beta-pictures', 'prompt': 'a blue square', 'response_format': 'b64_json'})
    assert refused.status_code >= 400 and fake.requests == []


SPEECH = {'id': 'lan-speech', 'name': 'Speech server', 'enabled': True, 'capabilities': ['speech'], 'config': {
    'cost_class': 'free-local',
    'connection': {'transport': 'http', 'dispatch': 'protocol', 'protocol': 'openai-compatible',
                   'address': f'http://{LAN}.6:8880', 'credentials': {},
                   'remote_dispatch': {'enabled': True, 'version': 1, 'transport': 'private_plaintext',
                                       'private_cidrs': [NETWORK], 'tasks': ['speech']}},
    'offerings': [{'id': 'lan-voice', 'kind': 'model', 'upstream': 'kokoro', 'tasks': [{'task': 'speech', 'evidence': 'declared'}],
                   'selection': {'mode': 'request_field'}}]}}


def _wav():
    import io, wave
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b'\x00\x00' * 80)
    return buffer.getvalue()


def test_speech_job_on_private_host_via_fake(gateway, fake_upstreams, monkeypatch, tmp_path):
    """A buffered speech request goes to a private host through the real remote boundary and the http-v1 executor."""
    _jobs_home(monkeypatch, tmp_path)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    from tests.test_owner_engine_chain_smell import _write_headers
    registry = owner.get('/admin/api/operator/service-registry').json()['service_registry']
    registry['services'][SPEECH['id']] = copy.deepcopy(SPEECH)
    stored = owner.put('/admin/api/operator/service-registry', headers=_write_headers(owner), json=registry)
    assert stored.status_code == 200, stored.text
    seen = []

    def server(request):
        seen.append(request)
        return httpx.Response(200, content=_wav(), headers={'Content-Type': 'audio/wav'})

    _fake_wire(fake_upstreams, f'{LAN}.6:8880', server)
    made = owner.post('/v1/audio/speech', headers={'authorization': f'Bearer {key}'},
                      json={'model': 'lan-voice', 'input': 'hello there', 'voice': 'af_bella', 'response_format': 'wav'})
    assert made.status_code == 200, made.text
    assert made.content.startswith(b'RIFF')
    assert [(r.method, str(r.url)) for r in seen] == [('POST', f'http://{LAN}.6:8880/v1/audio/speech')]
    assert json.loads(seen[0].content)['input'] == 'hello there'
    assert 'authorization' not in {name.lower() for name in seen[0].headers}  # the caller's key stops at the gateway

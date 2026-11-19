"""The sdapi dialect as data: switch the checkpoint, read the selection back, then generate.

A fake server speaks the dialect. Checkpoint names here are invented.
"""
import base64
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import media_jobs, receipt_store
from gateway.engine_protocol import description, executor, inventory, transport
from tests.support.access import authenticated
from tests.support.fake_upstream import HandlerTransport
from tests.support.protocol_upstream import png

ALPHA = 'alpha-v1.safetensors [aaaa1111]'
BETA = 'beta-v2.safetensors [bbbb2222]'
GHOST = 'ghost-v9.safetensors [cccc3333]'


class FakeSdapi:
    """A server of the sdapi dialect. It keeps one selected checkpoint, as the real servers do."""

    def __init__(self):
        self.requests = []
        self.checkpoints = [ALPHA, BETA]
        self.selected = ALPHA
        self.ignore_switch = False
        self.generated_with = []
        self.image = png()

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == '/sdapi/v1/sd-models':
            return httpx.Response(200, json=[{'title': t, 'model_name': t.split(' ')[0].rsplit('.', 1)[0], 'hash': t[-9:-1],
                                              'sha256': None, 'filename': f'/models/{t.split(" ")[0]}', 'config': None}
                                             for t in self.checkpoints])
        if path == '/sdapi/v1/options' and request.method == 'GET':
            return httpx.Response(200, json={'samples_format': 'png', 'sd_model_checkpoint': self.selected, 'sd_vae': 'Automatic'})
        if path == '/sdapi/v1/options' and request.method == 'POST':
            asked = json.loads(request.content)['sd_model_checkpoint']
            # A server given an unknown name answers 200 and keeps what it had.
            if not self.ignore_switch and asked in self.checkpoints:
                self.selected = asked
            return httpx.Response(200, json=None)
        if path == '/sdapi/v1/txt2img':
            self.generated_with.append(self.selected)
            return httpx.Response(200, json={'images': [base64.b64encode(self.image).decode()],
                                             'parameters': {}, 'info': json.dumps({'sd_model_name': self.selected})})
        if path.startswith('/zz-not-a-route'):  # the check's made-up path
            return httpx.Response(404, json={'detail': 'Not Found'})
        raise AssertionError((request.method, str(request.url), request.content))


def record(address='http://localhost:7860'):
    def offering(public, title):
        return {'id': public, 'kind': 'model', 'upstream': title,
                'tasks': [{'task': 'image', 'evidence': 'declared'}],
                'selection': {'mode': 'switch', 'switch': {
                    'call': 'choose', 'args': {'sd_model_checkpoint': title}, 'observe_selected': 'selected',
                    'exclusive_scope': 'engine', 'drain_timeout_s': 0.02, 'queue_wait_s': 0.02, 'on_failure': 'fail'}}}
    return {'id': 'sd-local', 'name': 'Picture server', 'enabled': True, 'config': {
        'connection': {'transport': 'http', 'dispatch': 'protocol', 'protocol': 'sdapi', 'address': address,
                       'credentials': {}, 'timeouts': {'connect_s': 2, 'first_byte_s': 10, 'total_s': 30}},
        'observations': {'selected': {'op': 'selected'}},
        'offerings': [offering('sd-alpha', ALPHA), offering('sd-beta', BETA)]}}


@pytest.fixture
def engine(monkeypatch, tmp_path):
    fake = FakeSdapi()
    rec = record()
    registry = {'schema_version': 2, 'services': {rec['id']: rec}}
    monkeypatch.setattr(proxy.ADMIN_STORE, 'get_service_registry', lambda: registry)
    monkeypatch.setattr(inventory, 'DESCRIPTION_LOAD_S', 60.0)
    monkeypatch.setattr(inventory, '_docs_last_good', None)
    monkeypatch.setattr(inventory, '_docs_load', None)
    monkeypatch.setattr(inventory, '_cache', type(inventory._cache)())
    wire = transport.HttpxTransport.for_tests(HandlerTransport(fake))
    monkeypatch.setattr(executor, 'HttpxTransport', lambda: wire)
    monkeypatch.setattr(proxy, '_litellm_runtime_config', lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr(proxy, 'config', {**proxy.config, 'model_aliases': {}, 'backends': {}, 'roles': {}, 'models': []})
    monkeypatch.setattr(proxy, 'IMAGE_CACHE_DIR', tmp_path / 'assets')
    monkeypatch.setattr(proxy, 'IMAGE_MEDIA_JOB_PAYLOAD_DIR', tmp_path / 'payloads')
    original_record = receipt_store.record
    monkeypatch.setattr(receipt_store, 'record', lambda receipt: original_record(receipt, path=receipt_store.DB_PATH))
    monkeypatch.setattr(media_jobs, 'DB_PATH', tmp_path / 'jobs.db')
    monkeypatch.setattr(receipt_store, 'DB_PATH', tmp_path / 'receipts.db')
    media_jobs.init_db(path=media_jobs.DB_PATH)
    receipt_store.init_db(path=receipt_store.DB_PATH)
    monkeypatch.setattr(proxy, '_decision_models_for', lambda *a, **k: [])
    with authenticated():
        yield SimpleNamespace(record=rec, fake=fake, client=TestClient(proxy.app), registry=registry)


def generate(engine, model, **extra):
    return engine.client.post('/v1/images/generations', json={'model': model, 'prompt': 'a blue square', 'response_format': 'b64_json', **extra})


def wire(engine):
    return [(r.method, r.url.path) for r in engine.fake.requests]


def test_description_validates():
    doc = description.load_registry()['sdapi']
    assert doc.executor == 'http-v1'
    assert {'images', 'choose', 'selected', 'models'} <= set(doc.operations)


def test_switch_then_generate_verifies_selected(engine):
    response = generate(engine, 'sd-beta', size='64x64')
    assert response.status_code == 200, response.text
    assert base64.b64decode(response.json()['data'][0]['b64_json']) == engine.fake.image
    assert engine.fake.generated_with == [BETA]
    assert wire(engine) == [('GET', '/sdapi/v1/options'), ('POST', '/sdapi/v1/options'),
                            ('GET', '/sdapi/v1/options'), ('POST', '/sdapi/v1/txt2img')]
    assert json.loads(engine.fake.requests[1].content) == {'sd_model_checkpoint': BETA}
    assert json.loads(engine.fake.requests[3].content) == {
        'prompt': 'a blue square', 'width': 64, 'height': 64, 'send_images': True, 'save_images': False}


def test_unknown_checkpoint_never_sent(engine):
    response = generate(engine, 'sd-gamma')
    assert response.status_code >= 400
    assert not engine.fake.requests


def test_a_switch_the_server_ignores_is_an_error_and_never_an_image(engine):
    # The server lists the checkpoint but keeps its old selection when asked to change it.
    engine.fake.ignore_switch = True
    response = generate(engine, 'sd-beta')
    assert response.status_code == 503, response.text
    assert response.headers['retry-after']
    assert 'b64_json' not in response.text
    assert engine.fake.generated_with == []
    assert all(r.url.path != '/sdapi/v1/txt2img' for r in engine.fake.requests)


def test_a_checkpoint_the_server_no_longer_holds_never_gets_an_image(engine):
    # The record offers it; the server dropped it. The server answers the switch with 200 and keeps what it had.
    engine.fake.checkpoints = [ALPHA]
    response = generate(engine, 'sd-beta')
    assert response.status_code == 503, response.text
    assert engine.fake.generated_with == []


def test_the_offering_already_selected_is_not_switched(engine):
    response = generate(engine, 'sd-alpha')
    assert response.status_code == 200, response.text
    assert engine.fake.generated_with == [ALPHA]
    assert [r.method for r in engine.fake.requests if r.url.path == '/sdapi/v1/options'] == ['GET']


def txt2img_bodies(engine):
    return [json.loads(r.content) for r in engine.fake.requests if r.url.path == '/sdapi/v1/txt2img']


def test_an_openai_size_reaches_the_server_as_width_and_height(engine):
    response = generate(engine, 'sd-alpha', size='1024x1024')
    assert response.status_code == 200, response.text
    [sent] = txt2img_bodies(engine)
    assert (sent['width'], sent['height']) == (1024, 1024)


@pytest.mark.parametrize('spelling, expected', [('512X768', (512, 768)), ('512 x 768', (512, 768)), (' 640x480 ', (640, 480))])
def test_the_size_spellings_of_the_image_edit_route_work_here_too(engine, spelling, expected):
    response = generate(engine, 'sd-alpha', size=spelling)
    assert response.status_code == 200, response.text
    [sent] = txt2img_bodies(engine)
    assert (sent['width'], sent['height']) == expected


@pytest.mark.parametrize('extra', [{}, {'size': 'auto'}])
def test_no_size_or_auto_sends_no_width_or_height(engine, extra):
    response = generate(engine, 'sd-alpha', **extra)
    assert response.status_code == 200, response.text
    [sent] = txt2img_bodies(engine)
    assert 'width' not in sent and 'height' not in sent


@pytest.mark.parametrize('size', ['big', '0x10', '10', '1024x', '10x-5', 1024, ['1', '2']])
def test_a_bad_size_is_a_400_naming_size_and_nothing_reaches_the_server(engine, size):
    response = generate(engine, 'sd-beta', size=size)
    assert response.status_code == 400, response.text
    error = response.json()['error']
    assert error['param'] == 'size'
    assert error['message'] == "size must be 'auto' or WIDTHxHEIGHT with positive integers"
    assert engine.fake.requests == []  # not even the checkpoint switch


def test_an_omitted_response_format_or_url_returns_a_served_url(engine):
    for extra in ({}, {'response_format': 'url'}):
        body = {'model': 'sd-alpha', 'prompt': 'a blue square', **extra}
        response = engine.client.post('/v1/images/generations', json=body)
        assert response.status_code == 200, response.text
        [item] = response.json()['data']
        assert item['url'] and 'b64_json' not in item
        # The url is one the gateway serves, and it holds the image the server made.
        parts = urlsplit(item['url'])
        served = engine.client.get(parts.path + ('?' + parts.query if parts.query else ''))
        assert served.status_code == 200 and served.content == engine.fake.image


def test_an_unknown_model_is_the_same_not_found_answer_other_engines_give(engine):
    response = generate(engine, 'sd-gamma')
    assert response.status_code == 404, response.text
    assert response.json()['error']['code'] == 'model_not_found'
    assert not engine.fake.requests


# ---- the definition: add from data, pick from a list, get an image from the picked checkpoint ------------------------

import asyncio  # noqa: E402

from gateway import admin_store, engine_check  # noqa: E402
from gateway.engine_check import Answer, KeyInput  # noqa: E402
from gateway.engine_definitions import load_engine_catalog  # noqa: E402
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: E402,F401 - fixture
from tests.test_added_speech_engine_serves_speech import _app_key  # noqa: E402
from tests.test_signin_who_can_enter import (  # noqa: E402,F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

TOKEN = 't0k3n'


def _definition():
    definition = load_engine_catalog().definition('sdapi')
    assert definition is not None, 'no definition named sdapi'
    return definition


def _sd_models_reply(titles):
    return [{'title': t, 'model_name': t.split('.')[0], 'hash': 'aaaa1111', 'sha256': None, 'filename': 'x', 'config': None}
            for t in titles]


def test_the_definition_lists_checkpoints_from_a_fake_sd_models_reply(monkeypatch):
    monkeypatch.setattr(engine_check, '_baseline_token', lambda: TOKEN)
    fake = FakeSdapi()
    seen = []

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        seen.append((method, url.split('7860', 1)[1]))
        reply = fake(httpx.Request(method, url, content=content))
        return Answer(method=method, url=url, status=reply.status_code, headers=dict(reply.headers), body=reply.content)

    catalog = load_engine_catalog()
    check = asyncio.run(engine_check.run_check(catalog, _definition(), {'address': 'http://127.0.0.1:7860'},
                                               key=KeyInput(), fetch=fetch))
    assert check['outcome'] == 'ok', check['findings']
    assert [row['id'] for row in check['models']] == [ALPHA, BETA]
    assert [(row['id'], row['ticked']) for row in check['capabilities']] == [('images', True)]
    # Nothing here can start a generation: the check reads two lists and sends empty requests to a made-up path only.
    assert ('POST', '/sdapi/v1/txt2img') not in seen and ('POST', '/sdapi/v1/options') not in seen
    assert {path for method, path in seen if method == 'POST'} <= {f'/zz-not-a-route-{TOKEN}'}


def test_a_server_without_the_web_api_is_not_recognised(monkeypatch):
    monkeypatch.setattr(engine_check, '_baseline_token', lambda: TOKEN)

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        return Answer(method=method, url=url, status=404, headers={}, body=b'{"detail":"Not Found"}')

    check = asyncio.run(engine_check.run_check(load_engine_catalog(), _definition(), {'address': 'http://127.0.0.1:7860'},
                                               key=KeyInput(), fetch=fetch))
    assert not [row for row in check['capabilities'] if row['ticked']]
    assert check['models'] == []


def _sdapi_server(fakes):
    """The check's reads on a real loopback port: the options read and the checkpoint list. Generation uses ``FakeSdapi``."""
    not_found = {'status': 404, 'json': {'detail': 'Not Found'}}

    def ex(name, method, path, response):
        return {'id': name, 'match': {'method': method, 'path': path}, 'response': response, 'repeat': True, 'required': False}

    return fakes.loopback(scenario_from_data({
        'protocol': 'engine-probe', 'name': 'sdapi-server',
        'provenance': {'source': 'hand-written', 'reference': 'SillyTavern src/endpoints/stable-diffusion.js reads GET /sdapi/v1/sd-models and /sdapi/v1/options'},
        'exchanges': [
            ex('baseline-get', 'GET', '/zz-not-a-route-{{token}}', not_found),
            ex('baseline-post', 'POST', '/zz-not-a-route-{{token}}', not_found),
            ex('options', 'GET', '/sdapi/v1/options', {'status': 200, 'json': {'sd_model_checkpoint': ALPHA}}),
            ex('sd-models', 'GET', '/sdapi/v1/sd-models', {'status': 200, 'json': _sd_models_reply([ALPHA, BETA])}),
        ],
    }, token=TOKEN))


def _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path, *, tick=(BETA,)):
    """The owner checks the server, ticks checkpoints, and saves. Returns what later steps need."""
    from tests.support.engine_dns import use_fake_engine_dns
    use_fake_engine_dns(monkeypatch)
    monkeypatch.setattr(engine_check, '_baseline_token', lambda: TOKEN)
    for module in (receipt_store, media_jobs):
        db = tmp_path / f'{module.__name__}.db'
        module.init_db(db)
        monkeypatch.setattr(module, 'DB_PATH', db)
    monkeypatch.setattr(proxy, 'IMAGE_CACHE_DIR', tmp_path / 'images')
    monkeypatch.setattr(proxy, 'IMAGE_MEDIA_JOB_PAYLOAD_DIR', tmp_path / 'payloads')
    server = _sdapi_server(fake_upstreams)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)

    # The owner types the address; the server reports its checkpoints.
    checked = owner.post('/admin/api/engines/check', headers=csrf_for(owner),
                         json={'definition': 'sdapi', 'values': {'address': server.base_url}})
    assert checked.status_code == 200, checked.text
    check = checked.json()['check']
    assert check['outcome'] == 'ok', check['findings']
    assert [row['id'] for row in check['models']] == [ALPHA, BETA]
    picked = [row for row in check['models'] if row['id'] in tick]

    etag = owner.get('/admin/api/operator/service-registry').headers.get('etag', '"os-0"')
    created = owner.post('/admin/api/operator/services', headers={**csrf_for(owner), 'If-Match': etag}, json={
        'definition': 'sdapi', 'name': 'garage-pictures', 'values': {'address': server.base_url},
        'capabilities': ['images'], 'models': picked})
    assert created.status_code == 201, created.text
    return SimpleNamespace(owner=owner, key=key, server=server, check=check, picked=picked, service_id='garage-pictures',
                           all_models=check['models'])


def _saved(gateway, service_id='garage-pictures'):
    return admin_store.get_service_registry_with_generation(path=gateway.path)['value']['services'][service_id]


def test_owner_adds_the_server_from_data_picks_a_checkpoint_and_gets_its_image(gateway, fake_upstreams, monkeypatch, tmp_path):
    added = _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path)
    owner, key, picked = added.owner, added.key, added.picked
    record = _saved(gateway)
    config = record['config']
    assert config['connection']['dispatch'] == 'protocol' and config['connection']['protocol'] == 'sdapi'
    [offering] = config['offerings']
    assert offering['upstream'] == BETA and offering['tasks'] == [{'task': 'image', 'evidence': 'declared'}]
    assert offering['selection']['switch']['args'] == {'sd_model_checkpoint': BETA}
    assert record['observations']['selected'] == {'op': 'selected'}

    # Generation goes to a stateful fake of the server; the saved record decides the calls.
    fake = FakeSdapi()
    wire_transport = transport.HttpxTransport.for_tests(HandlerTransport(fake))
    monkeypatch.setattr(executor, 'HttpxTransport', lambda: wire_transport)
    made = owner.post('/v1/images/generations', headers={'authorization': f'Bearer {key}'},
                      json={'model': picked[0]['public_name'], 'prompt': 'a blue square', 'response_format': 'b64_json'})
    assert made.status_code == 200, made.text
    assert base64.b64decode(made.json()['data'][0]['b64_json']) == fake.image
    assert fake.generated_with == [BETA]
    assert [(r.method, r.url.path) for r in fake.requests] == [
        ('GET', '/sdapi/v1/options'), ('POST', '/sdapi/v1/options'), ('GET', '/sdapi/v1/options'), ('POST', '/sdapi/v1/txt2img')]


def _page_edit(added, **body):
    """What the engine page sends for a saved server: the definition, the address, and the models it lists."""
    from tests.test_engine_admin_routes import _edit
    sent = {'definition': 'sdapi', 'values': {'address': added.server.base_url}, **body}
    return _edit(added.owner, added.service_id, sent)


def _rows(added, *ticked):
    return [{'id': row['id'], 'public_name': row['public_name'], 'enabled': row['id'] in ticked} for row in added.all_models]


def _offered(gateway):
    """(public id, checkpoint, switch args) of every saved offering, and the saved model rows' checkpoints."""
    config = _saved(gateway)['config']
    return ([(o['id'], o['upstream'], o['selection']['switch']['args']) for o in config['offerings']],
            [(m['upstream_model'], m.get('enabled', True)) for m in config['models']])


def _generate_as_owner(added, monkeypatch, model):
    fake = FakeSdapi()
    monkeypatch.setattr(executor, 'HttpxTransport', lambda: transport.HttpxTransport.for_tests(HandlerTransport(fake)))
    made = added.owner.post('/v1/images/generations', headers={'authorization': f'Bearer {added.key}'},
                            json={'model': model, 'prompt': 'a blue square', 'response_format': 'b64_json'})
    return made, fake


def test_renaming_a_saved_server_keeps_its_checkpoints_and_offerings(gateway, fake_upstreams, monkeypatch, tmp_path):
    from tests.test_engine_admin_routes import _edit
    added = _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path)
    before = _offered(gateway)
    assert _edit(added.owner, added.service_id, {'name': 'Renamed plainly'}).status_code == 200
    assert _saved(gateway)['name'] == 'Renamed plainly' and _offered(gateway) == before
    renamed = _page_edit(added, name='Renamed on the page')
    assert renamed.status_code == 200, renamed.text
    assert _saved(gateway)['name'] == 'Renamed on the page' and _offered(gateway) == before
    made, fake = _generate_as_owner(added, monkeypatch, added.picked[0]['public_name'])
    assert made.status_code == 200 and fake.generated_with == [BETA]


def test_the_page_may_edit_checkpoints_of_a_server_it_added(gateway, fake_upstreams, monkeypatch, tmp_path):
    added = _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path)
    view = added.owner.get(f'/admin/api/engines/{added.service_id}').json()['engine']
    assert view['can_edit_offerings'] is True and view['offering_edit_reason'] is None


def test_ticking_a_new_checkpoint_list_rebuilds_the_offerings_from_the_definition(gateway, fake_upstreams, monkeypatch, tmp_path):
    added = _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path)
    edited = _page_edit(added, capabilities=['images'], models=_rows(added, ALPHA, BETA))
    assert edited.status_code == 200, edited.text
    offerings, rows = _offered(gateway)
    assert [(o[1], o[2]) for o in offerings] == [(ALPHA, {'sd_model_checkpoint': ALPHA}), (BETA, {'sd_model_checkpoint': BETA})]
    assert rows == [(ALPHA, True), (BETA, True)]
    # The same offering a create builds: every key comes from the definition's template.
    config = _saved(gateway)['config']
    template = _definition()['record']['dispatch']['offering']['selection']['switch']
    assert config['offerings'][0]['selection']['switch']['drain_timeout_s'] == template['drain_timeout_s']
    made, fake = _generate_as_owner(added, monkeypatch, added.all_models[0]['public_name'])
    assert made.status_code == 200, made.text
    assert fake.generated_with == [ALPHA]


def test_unticking_a_checkpoint_removes_its_offering(gateway, fake_upstreams, monkeypatch, tmp_path):
    added = _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path, tick=(ALPHA, BETA))
    assert [o[1] for o in _offered(gateway)[0]] == [ALPHA, BETA]
    edited = _page_edit(added, capabilities=['images'], models=_rows(added, ALPHA))
    assert edited.status_code == 200, edited.text
    offerings, rows = _offered(gateway)
    assert [o[1] for o in offerings] == [ALPHA] and rows == [(ALPHA, True), (BETA, False)]
    gone, fake = _generate_as_owner(added, monkeypatch, added.all_models[1]['public_name'])
    assert gone.status_code >= 400, gone.text  # the name is no longer an offering of this server
    assert fake.requests == []  # and nothing is sent to it, not even a checkpoint switch


def test_a_models_edit_without_the_definition_rebuilds_the_offerings_too(gateway, fake_upstreams, monkeypatch, tmp_path):
    # A script or a tool may send only the models. They used to change the model rows and leave the offerings as they were.
    from tests.test_engine_admin_routes import _edit
    added = _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path)
    edited = _edit(added.owner, added.service_id, {'capabilities': ['images'], 'models': _rows(added, ALPHA, BETA)})
    assert edited.status_code == 200, edited.text
    offerings, rows = _offered(gateway)
    assert [o[1] for o in offerings] == [ALPHA, BETA] and [r[0] for r in rows] == [ALPHA, BETA]


def test_offerings_changed_by_hand_are_refused_for_a_models_edit_and_nothing_changes(gateway, fake_upstreams, monkeypatch, tmp_path):
    from tests.test_engine_admin_routes import _edit, _put_registry, _registry
    added = _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path)
    registry = _registry(gateway)
    registry['services'][added.service_id]['config']['offerings'][0]['selection']['switch']['drain_timeout_s'] = 7
    _put_registry(gateway, registry)
    view = added.owner.get(f'/admin/api/engines/{added.service_id}').json()['engine']
    assert view['can_edit_offerings'] is False and view['offering_edit_reason']
    before = _saved(gateway)
    for send in (lambda: _page_edit(added, capabilities=['images'], models=_rows(added, ALPHA, BETA)),
                 lambda: _edit(added.owner, added.service_id, {'models': _rows(added, ALPHA, BETA)})):
        refused = send()
        assert refused.status_code == 400, refused.text
        assert refused.json()['error']['field'] == 'models' and refused.json()['error']['message']
    assert _saved(gateway) == before


def test_moving_a_saved_server_to_another_local_address_checks_it_and_keeps_the_offerings(gateway, fake_upstreams, monkeypatch, tmp_path):
    from tests.test_engine_admin_routes import _edit
    added = _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path)
    before = _offered(gateway)
    moved = _sdapi_server(fake_upstreams)
    edited = _edit(added.owner, added.service_id, {'definition': 'sdapi', 'values': {'address': moved.base_url}})
    assert edited.status_code == 200, edited.text
    assert _saved(gateway)['config']['connection']['address'] == moved.base_url
    assert _offered(gateway) == before


def test_moving_a_saved_server_to_another_computer_is_refused_in_words_before_anything_is_sent(gateway, fake_upstreams, monkeypatch, tmp_path):
    from tests.test_engine_admin_routes import _edit
    added = _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path)
    before = _saved(gateway)
    refused = _edit(added.owner, added.service_id, {'definition': 'sdapi', 'values': {'address': 'http://203.0.113.9:7860'}})
    assert refused.status_code == 422, refused.text
    assert refused.json()['error']['code'] == 'engine_check_failed' and refused.json()['error']['field'] == 'address'
    assert refused.json()['error']['message'] == 'This kind of server can be added only when it runs on this computer.'
    assert _saved(gateway) == before


def test_the_check_says_before_save_that_only_this_computer_works(gateway, fake_upstreams, monkeypatch, tmp_path):
    # Nothing listens at 203.0.113.9: the hermetic guard fails any attempt to connect, so a pass here means nothing was sent.
    added = _owner_adds(gateway, fake_upstreams, monkeypatch, tmp_path)
    owner = added.owner
    checked = owner.post('/admin/api/engines/check', headers=csrf_for(owner),
                         json={'definition': 'sdapi', 'values': {'address': 'http://203.0.113.9:7860'}})
    assert checked.status_code == 200, checked.text
    check = checked.json()['check']
    assert check['outcome'] == 'error' and check['models'] == []
    [finding] = check['findings']
    assert finding['code'] == 'local_only' and finding['severity'] == 'error' and finding['field'] == 'address'
    assert finding['what'] == 'This kind of server can be added only when it runs on this computer.'
    assert 'http://203.0.113.9:7860' in finding['why'] and '127.0.0.1' in finding['fix']
    etag = owner.get('/admin/api/operator/service-registry').headers.get('etag', '"os-0"')
    created = owner.post('/admin/api/operator/services', headers={**csrf_for(owner), 'If-Match': etag}, json={
        'definition': 'sdapi', 'name': 'lan-pictures', 'values': {'address': 'http://203.0.113.9:7860'},
        'capabilities': ['images'], 'models': added.picked})
    assert created.status_code == 422 and created.json()['error']['code'] == 'engine_check_failed', created.text
    assert 'lan-pictures' not in admin_store.get_service_registry_with_generation(path=gateway.path)['value']['services']

"""Regression contracts for partial availability and public executor parity."""
import asyncio
import copy
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import yaml

import proxy
from gateway import receipt_store
from gateway.engine_protocol import description, dispatch, executor, transport
from gateway.engine_protocol.errors import ProtocolError
from tests.test_engine_protocol_public import engine, chat, FIXTURE
from tests.support.fake_upstream import HandlerTransport
from tests.support.access import access_context


def wire(monkeypatch, handler):
    client = transport.HttpxTransport.for_tests(HandlerTransport(handler))
    monkeypatch.setattr(executor, 'HttpxTransport', lambda: client)


def test_one_unreachable_engine_does_not_remove_healthy_models(engine, monkeypatch):
    bad = copy.deepcopy(engine.record)
    bad['id'] = 'down'
    bad['config']['connection']['address'] = 'http://down.test:9100'
    engine.registry['services']['down'] = bad
    original = engine.fake.__call__
    def handler(request):
        if request.url.host == 'down.test':
            raise ProtocolError('socket internals /srv/private/config', code='transport_failed')
        return original(request)
    wire(monkeypatch, handler)
    response = engine.client.get('/v1/models')
    assert response.status_code == 200, response.text
    assert 'prism-chat' in {r['id'] for r in response.json()['data']}
    assert 'private' not in response.text


def test_inventory_calls_are_concurrent_bounded_and_cached(engine, monkeypatch):
    from gateway.engine_protocol import inventory
    inventory._cache.clear()
    active = peak = calls = 0
    original = dispatch.Runtime.invoke
    async def invoke(self, op, body):
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.05)
            return await original(self, op, body)
        finally:
            active -= 1
    monkeypatch.setattr(dispatch.Runtime, 'invoke', invoke)
    for n in range(3):
        row = copy.deepcopy(engine.record)
        row['id'] = f'engine-{n}'
        engine.registry['services'][row['id']] = row
    async def go():
        await proxy._protocol_models_payload(access_context())
        await proxy._protocol_models_payload(access_context())
    asyncio.run(go())
    assert peak == 4
    assert calls == 4


def test_bad_description_disables_only_its_own_records(engine, monkeypatch, tmp_path):
    (tmp_path/'prism.yml').write_text((FIXTURE/'prism.yml').read_text())
    (tmp_path/'broken.yml').write_text('id: broken\n')
    # Recover the real loader from the fixture's closure.
    loader = description.load_registry.__closure__[0].cell_contents
    monkeypatch.setattr(description, 'load_registry', lambda: loader(tmp_path))
    assert chat(engine).status_code == 200
    response = engine.client.get('/v1/models')
    assert response.status_code == 200, response.text
    assert 'prism-chat' in {r['id'] for r in response.json()['data']}
    from gateway.admin_api import derive_admin_attention
    attention = derive_admin_attention({'protocol_issues': description.registry_problems()})
    assert any('broken.yml' in str(item) for item in attention)
    assert str(tmp_path) not in json.dumps(attention)


@pytest.mark.parametrize('op,payload', [('chat',{}), ('chat',{'choices':[{'message':None}]}),
    ('embeddings', {'data':[None]}), ('embeddings', {'object':'list','data':[{'embedding':'oops'}]})])
def test_bad_public_shape_returns_typed_502(engine, monkeypatch, op, payload):
    data = yaml.safe_load((FIXTURE/'prism.yml').read_text())
    data['operations'][op]['response'] = {'framing':'json'}
    doc = description.parse_description(data, source='prism.yml')
    monkeypatch.setattr(description, 'load_registry', lambda: {'prism':doc})
    wire(monkeypatch, lambda r: httpx.Response(200, json=payload))
    response = chat(engine) if op == 'chat' else engine.client.post('/v1/embeddings', json={'model':'prism-embed','input':'hi'})
    assert response.status_code == 502, response.text
    assert response.json()['error']['code'] == 'malformed_response'


def test_public_errors_do_not_disclose_internal_paths_or_raw_text():
    response = proxy._protocol_failure(ProtocolError('/srv/secret/file: raw transport failure', code='invalid_description'))
    assert response.status_code == 502
    assert b'/srv' not in response.body and b'raw transport' not in response.body
    assert json.loads(response.body)['error']['code'] == 'invalid_description'


def test_chat_receipt_has_legacy_stream_shape_and_limit_fields(engine):
    response = chat(engine, stream=True, max_tokens=19)
    assert response.status_code == 200
    [row] = receipt_store.recent(path=receipt_store.DB_PATH)
    assert row['intent'] == 'chat.completions'
    assert row['stream'] is True
    assert row['effective_output_limit'] == 19
    assert row['metadata']['request_shape']
    assert {'accepted','outer_egress'} <= {stage['stage'] for stage in row['metadata']['request_shape_stages']}


@pytest.mark.parametrize('known_namespace', [False, True])
def test_unknown_chat_extension_matches_legacy_validation(engine, known_namespace):
    from gateway.chat_extensions import CHAT_EXTENSION_NAMESPACE, validate_chat_completions_extensions
    extensions = {CHAT_EXTENSION_NAMESPACE: {'unknown_input': True}} if known_namespace else {'unknown_namespace': {'x': True}}
    expected = validate_chat_completions_extensions({'extensions':extensions})
    response = chat(engine, extensions=extensions)
    assert response.status_code == (200 if expected.ok else 400), response.text
    if expected.ok:
        assert b'extensions' not in engine.fake.requests[-1].content
    else:
        assert not engine.fake.requests


def test_embedding_dimensions_are_removed_upstream_and_normalized(engine):
    response = engine.client.post('/v1/embeddings', json={'model':'prism-embed','input':'hi','dimensions':1})
    assert response.status_code == 200, response.text
    assert response.json()['data'][0]['embedding'] == [1.0]
    assert b'dimensions' not in engine.fake.requests[-1].content


def test_engine_description_is_loaded_outside_repo(engine, monkeypatch, tmp_path):
    root = tmp_path/'user-protocols'
    root.mkdir()
    import shutil
    shutil.copyfile(FIXTURE/'prism.yml', root/'prism.yml')
    shutil.copyfile(FIXTURE/'engine.json', root/'engine.json')
    engine.record.clear()
    engine.record.update(json.loads((root/'engine.json').read_text()))
    loader = description.load_registry.__closure__[0].cell_contents
    monkeypatch.setattr(description, 'load_registry', lambda: loader(root))
    assert not root.is_relative_to(Path(proxy.__file__).parent)
    assert chat(engine).status_code == 200
    assert engine.client.get('/v1/models').status_code == 200


def test_multiline_sse_held_cr_does_not_end_event_early():
    from gateway.engine_protocol.framing import SseDecoder, SseEvent
    decoder = SseDecoder()
    assert decoder.feed(b'data: one\r') == []
    assert decoder.feed(b'\ndata: two\r') == []
    assert decoder.feed(b'\n\r') == []
    assert decoder.feed(b'\n') == [SseEvent(None, b'one\ntwo')]


def test_protocol_image_entry_is_neutral():
    import inspect
    assert '_try_protocol_request' not in inspect.getsource(proxy.proxy_comfy_request)
    assert '_try_protocol_request' in inspect.getsource(proxy.proxy_image_request)


def test_inventory_timeout_leaves_healthy_results_and_status_issue(engine, monkeypatch):
    from gateway.engine_protocol import inventory
    monkeypatch.setattr(inventory, 'ENGINE_TIMEOUT_S', .04)
    monkeypatch.setattr(inventory, 'LIST_TIMEOUT_S', .08)
    original = dispatch.Runtime.invoke
    async def invoke(self, op, body):
        if self.target.engine_id == 'hung':
            await asyncio.sleep(5)
        return await original(self, op, body)
    monkeypatch.setattr(dispatch.Runtime, 'invoke', invoke)
    bad = copy.deepcopy(engine.record)
    bad['id'] = 'hung'
    engine.registry['services']['hung'] = bad
    started = time.monotonic()
    response = engine.client.get('/v1/models')
    assert response.status_code == 200
    assert time.monotonic() - started < 1
    assert 'prism-chat' in {r['id'] for r in response.json()['data']}
    assert any(row['engine_id'] == 'hung' for row in inventory.problems())


def test_user_descriptions_override_seeds_and_reload_independently(monkeypatch, tmp_path):
    seed = tmp_path/'seed'; user = tmp_path/'state'/'protocols'
    seed.mkdir(); user.mkdir(parents=True)
    text = (FIXTURE/'prism.yml').read_text()
    (seed/'prism.yml').write_text(text)
    (user/'prism.yml').write_text('id: prism\n')
    monkeypatch.setattr(description, 'REGISTRY_DIR', seed)
    monkeypatch.setenv('AZIZAM_PROTOCOLS_DIR', str(user))
    assert 'prism' not in description.load_registry()
    (user/'prism.yml').write_text(text)
    assert 'prism' in description.load_registry()
    assert not description.registry_problems()


def test_invalid_first_stream_chunk_is_a_typed_502(engine, monkeypatch):
    from tests.test_engine_protocol_executor import Chunks
    data = yaml.safe_load((FIXTURE/'prism.yml').read_text())
    stream = data['operations']['chat']['response']['stream']
    stream.pop('map')
    stream['terminal'] = {'sentinel':'[DONE]'}
    doc = description.parse_description(data, source='prism.yml')
    monkeypatch.setattr(description, 'load_registry', lambda: {'prism':doc})
    wire(monkeypatch, lambda r: httpx.Response(200, stream=Chunks([b'data: {}\n\ndata: [DONE]\n\n'])))
    response = chat(engine, stream=True)
    assert response.status_code == 502
    assert response.json()['error']['code'] == 'malformed_response'


def test_missing_unused_control_key_does_not_break_public_chat(engine, monkeypatch):
    conn = engine.record['config']['connection']
    conn['credentials'] = {'control': {'secret_ref':'os.environ/UNSET_CONTROL_R2',
        'origin':'http://prism.test:9100','header':'X-Control','scheme':''}}
    monkeypatch.delenv('UNSET_CONTROL_R2', raising=False)
    assert chat(engine).status_code == 200
    assert all('x-control' not in request.headers for request in engine.fake.requests)


def test_missing_inference_key_fails_closed_before_control_or_generation(engine, monkeypatch):
    from tests.test_engine_protocol_public import switching
    switching(engine)
    conn = engine.record['config']['connection']
    conn['credentials'] = {'inference': {'secret_ref':'os.environ/UNSET_INFERENCE_R2',
        'origin':'http://prism.test:9100','header':'X-Inference','scheme':''}}
    monkeypatch.delenv('UNSET_INFERENCE_R2', raising=False)
    response = chat(engine)
    assert response.status_code == 503, response.text
    assert response.json()['error']['code'] == 'engine_credential_unavailable'
    assert not engine.fake.requests


def test_description_validate_command_prints_all_problems(monkeypatch, tmp_path, capsys):
    path = tmp_path/'invalid.yml'
    path.write_text('protocol_version: 2\nid: Invalid\nexecutor: arbitrary\noperations: {}\n')
    monkeypatch.setattr('sys.argv', ['validate-protocol', str(path)])
    assert description.main() == 1
    output = capsys.readouterr().out
    assert 'invalid.yml' in output and 'protocol_version' in output and 'executor' in output


def test_total_inventory_deadline_reports_cancelled_engines(engine, monkeypatch):
    from gateway.engine_protocol import inventory
    inventory._cache.clear()
    inventory._issues.clear()
    monkeypatch.setattr(inventory, 'ENGINE_TIMEOUT_S', 5)
    monkeypatch.setattr(inventory, 'LIST_TIMEOUT_S', .03)
    closed = []
    async def hang(self, op, body):
        try:
            await asyncio.sleep(5)
        finally:
            closed.append(self.target.engine_id)
    monkeypatch.setattr(dispatch.Runtime, 'invoke', hang)
    response = engine.client.get('/v1/models')
    assert response.status_code == 200
    assert closed == [engine.record['id']]
    assert any(row['engine_id'] == engine.record['id'] for row in inventory.problems())


def test_later_invalid_stream_event_is_typed_without_done_and_receipt_stays_failed(engine, monkeypatch):
    from tests.test_engine_protocol_executor import Chunks
    data = yaml.safe_load((FIXTURE/'prism.yml').read_text())
    stream = data['operations']['chat']['response']['stream']
    stream.pop('map'); stream['terminal'] = {'sentinel':'[DONE]'}
    doc = description.parse_description(data, source='prism.yml')
    monkeypatch.setattr(description, 'load_registry', lambda: {'prism':doc})
    good = {'object':'chat.completion.chunk','id':'a','created':0,'model':'m',
        'choices':[{'index':0,'delta':{'content':'hi'},'finish_reason':None}]}
    wire(monkeypatch, lambda r: httpx.Response(200, stream=Chunks([
        b'data: ' + json.dumps(good).encode() + b'\n\n', b'data: {}\n\ndata: [DONE]\n\n'])))
    response = chat(engine, stream=True)
    assert response.status_code == 200
    assert b'malformed_response' in response.content
    assert b'[DONE]' not in response.content
    [receipt] = receipt_store.recent(path=receipt_store.DB_PATH)
    assert receipt['status'] == 'failed'


def test_cyclic_yaml_file_cannot_break_unrelated_description(tmp_path):
    (tmp_path/'prism.yml').write_text((FIXTURE/'prism.yml').read_text())
    (tmp_path/'cycle.yml').write_text('protocol_version: 1\nid: cycle\nexecutor: http-v1\noperations: &self\n  chat: *self\n')
    assert set(description.load_registry(tmp_path)) == {'prism'}
    assert any(row['file'] == 'cycle.yml' for row in description.registry_problems())


@pytest.mark.parametrize('op,payload', [
    ('chat', {'object':'chat.completion','id':'c','created':0,'model':'m','choices':[
        {'index':0,'message':{'role':'assistant','content':None,'tool_calls':[
            {'type':'function','function':{'name':'f','arguments':{}}}]},'finish_reason':'tool_calls'}]}),
    ('embeddings', {'object':'list','model':'m','data':[{'object':'embedding','index':0,'embedding':'%%%'}]})])
def test_malformed_tool_calls_and_encoded_embeddings_are_typed_502(engine, monkeypatch, op, payload):
    data = yaml.safe_load((FIXTURE/'prism.yml').read_text())
    data['operations'][op]['response'] = {'framing':'json'}
    doc = description.parse_description(data, source='prism.yml')
    monkeypatch.setattr(description, 'load_registry', lambda: {'prism':doc})
    wire(monkeypatch, lambda r: httpx.Response(200, json=payload))
    response = chat(engine) if op == 'chat' else engine.client.post('/v1/embeddings', json={'model':'prism-embed','input':'hi'})
    assert response.status_code == 502, response.text
    assert response.json()['error']['code'] == 'malformed_response'


def test_streamed_tool_metadata_can_precede_function_arguments():
    from gateway.engine_protocol.public_shape import validate
    chunk = {'object':'chat.completion.chunk','id':'c','created':0,'model':'m','choices':[
        {'index':0,'delta':{'tool_calls':[{'index':0,'id':'call-1','type':'function'}]},'finish_reason':None}]}
    assert validate(chunk, 'chat', stream=True) == chunk


def test_unrepresentable_embedding_number_is_a_typed_response_failure():
    from gateway.engine_protocol.public_shape import validate
    payload = {'object':'list','model':'m','data':[{'object':'embedding','index':0,'embedding':[10**400]}]}
    with pytest.raises(ProtocolError) as raised:
        validate(payload, 'embeddings')
    assert raised.value.code == 'malformed_response'

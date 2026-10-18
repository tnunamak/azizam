"""W6a round-3 regressions: D24 refusal at every level, description load budget, response shape, registry round trip."""
import asyncio
import copy
import json
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import proxy
from fastapi.testclient import TestClient
from gateway import media_jobs, receipt_store
from gateway import service_registry as sr
from gateway.engine_protocol import description, dispatch, executor, inventory, public_shape, transport
from gateway.engine_protocol.errors import ProtocolError
from tests.support.access import authenticated
from tests.support.fake_upstream import HandlerTransport
from tests.test_engine_protocol_executor import Chunks

REGISTRY = Path(__file__).resolve().parent.parent / 'registry/protocols'
OLLAMA = REGISTRY / 'ollama-native.yml'
LOCAL = {'dispatch': 'protocol', 'protocol': 'ollama-native', 'address': 'http://127.0.0.1:11434', 'credentials': {}}


def offering(i, **extra):
    return {'id': f'm{i}', 'kind': 'model', 'upstream': f'm{i}', 'tasks': [{'task': 'chat'}],
            'selection': {'mode': 'request_field'}, **extra}


def record(engine_id='e', *, connection=None, **extra):
    config = {'connection': dict(LOCAL, **(connection or {})), 'offerings': [offering(0)]}
    config.update(extra.pop('config', {}))
    return {'id': engine_id, 'enabled': True, 'config': config, **extra}


def refused(service):
    try:
        sr.validate_protocol_dispatch_local_only(service)
    except ValueError:
        return True
    return False


# --- H-1: every level of a record is checked; the allowlist is short ----------------------------

REFUSED_ROWS = [
    ('provider_type openrouter (not a remote-type name)', record(config={'provider_type': 'openrouter'})),
    ('cost_class openai', record(config={'cost_class': 'openai'})),
    ('cost_class unknown', record(config={'cost_class': 'unknown'})),
    ('provider_type on the record itself', dict(record(), provider_type='openrouter')),
    ('connection paid_remote', record(connection={'paid_remote': True})),
    ('connection cost_class paid-remote', record(connection={'cost_class': 'paid-remote'})),
    ('offering cost_class paid-remote', record(config={'offerings': [offering(0, cost_class='paid-remote')]})),
    ('offering provider_type openrouter', record(config={'offerings': [offering(0, provider_type='openrouter')]})),
    ('offering paid_remote', record(config={'offerings': [offering(0, paid_remote=True)]})),
    ('substrate kind llm-substrate on a local address', dict(record('my-router'), kind='llm-substrate')),
    ('record id litellm on a local address', record('litellm')),
    ('execution_substrate litellm on a local address', record(config={'execution_substrate': 'litellm'})),
    ('paid_remote on the record itself', dict(record(), paid_remote=True)),
    ('userinfo in address', record(connection={'address': 'http://evil.example\\@127.0.0.1:9'})),
    ('userinfo in address (at sign)', record(connection={'address': 'http://127.0.0.1@evil.example:9'})),
    ('tab in address', record(connection={'address': 'http://evil.example\t@127.0.0.1:9'})),
    ('remote LAN address', record(connection={'address': 'http://192.168.1.5:11434'})),
    ('public address', record(connection={'address': 'https://api.example.com'})),
]

ACCEPTED_ROWS = [
    ('loopback control', record()),
    ('provider_type local', record(config={'provider_type': 'local'})),
    ('cost_class free-local', record(config={'cost_class': 'free-local', 'provider_type': 'local'})),
]


@pytest.mark.parametrize('label,service', REFUSED_ROWS, ids=[row[0] for row in REFUSED_ROWS])
def test_D24_paid_remote_or_substrate_markers_are_refused_at_every_level(label, service):
    assert refused(service), label


@pytest.mark.parametrize('label,service', ACCEPTED_ROWS, ids=[row[0] for row in ACCEPTED_ROWS])
def test_D24_local_records_stay_dispatchable(label, service):
    assert not refused(service), label


def test_D24_dispatch_resolve_refuses_the_same_record_at_request_time():
    registry = {'services': {'paid': record('paid', config={'provider_type': 'openrouter'})}}
    with pytest.raises(ProtocolError) as caught:
        dispatch.resolve(registry, 'm0', 'chat')
    assert caught.value.code == 'protocol_dispatch_refused'


def test_D24_configuration_problems_report_the_substrate_record():
    registry = {'services': {'my-router': dict(record('my-router'), kind='llm-substrate')}}
    problems = inventory.configuration_problems(registry)
    assert [problem['engine_id'] for problem in problems] == ['my-router']


# --- M-1: description loading is bounded by the listing deadline and by the alias rule ----------

def _alias_bomb(levels, fan):
    lines = ['x0: &a0 [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]']
    prev = 'a0'
    for i in range(1, levels):
        lines.append(f'x{i}: &a{i} [' + ', '.join([f'*{prev}'] * fan) + ']')
        prev = f'a{i}'
    return 'protocol_version: 1\nid: bomb\nexecutor: http-v1\n' + '\n'.join(lines) + '\nx-bomb: *' + prev + '\n'


def test_M1_yaml_alias_fan_out_is_refused_before_it_expands():
    with pytest.raises(description.DescriptionError, match='alias'):
        description.parse_description_text(_alias_bomb(levels=6, fan=10), source='bomb.yml')


def test_M1_a_single_yaml_anchor_is_refused_even_without_an_alias():
    text = OLLAMA.read_text().replace('title: Ollama native HTTP API', 'title: &t Ollama native HTTP API')
    with pytest.raises(description.DescriptionError, match='alias|anchor'):
        description.parse_description_text(text, source='anchored.yml')


def _registry(n):
    return {'services': {f'e{i}': record(f'e{i}', config={'offerings': [offering(i)]}) for i in range(n)}}


@pytest.fixture
def fresh_inventory(monkeypatch):
    monkeypatch.setattr(inventory, '_cache', OrderedDict())
    monkeypatch.setattr(inventory, '_issues', {})
    monkeypatch.setattr(inventory, '_docs_last_good', None, raising=False)
    monkeypatch.setattr(inventory, '_docs_load', None, raising=False)


@pytest.fixture
def ollama_doc():
    return description.parse_description_text(OLLAMA.read_text(), source='ollama-native.yml')


def _invoke_ids(monkeypatch, ids):
    async def invoke(self, op, body):
        return SimpleNamespace(body={'data': [{'id': ids[self.target.engine_id]}]}, asset=None)
    monkeypatch.setattr(dispatch.Runtime, 'invoke', invoke)


def test_M1_slow_description_load_cannot_outlive_the_listing_deadline(monkeypatch, fresh_inventory):
    def slow(directory=None):
        time.sleep(3)
        return {}
    monkeypatch.setattr(description, 'load_registry', slow)

    async def timed():
        started = time.perf_counter()
        out = await inventory.models(_registry(1))
        return time.perf_counter() - started, out
    elapsed, out = asyncio.run(timed())
    assert elapsed < inventory.LIST_TIMEOUT_S + 0.5, elapsed
    assert out == []


def test_M1_timed_out_reload_serves_the_last_good_description(monkeypatch, fresh_inventory, ollama_doc):
    monkeypatch.setattr(description, 'load_registry', lambda directory=None: {'ollama-native': ollama_doc})
    _invoke_ids(monkeypatch, {'e0': 'm0'})
    asyncio.run(inventory.models(_registry(1)))
    monkeypatch.setattr(inventory, 'CACHE_TTL_S', 0.0)

    def slow(directory=None):
        time.sleep(3)
        return {}
    monkeypatch.setattr(description, 'load_registry', slow)

    async def timed():
        started = time.perf_counter()
        out = await inventory.models(_registry(1))
        return time.perf_counter() - started, out
    elapsed, out = asyncio.run(timed())
    assert elapsed < inventory.LIST_TIMEOUT_S + 0.5, elapsed
    assert [item['id'] for item in out] == ['m0']


def test_L4_a_record_that_cannot_be_serialised_is_isolated(monkeypatch, fresh_inventory, ollama_doc):
    monkeypatch.setattr(description, 'load_registry', lambda directory=None: {'ollama-native': ollama_doc})
    _invoke_ids(monkeypatch, {'e0': 'm0', 'e1': 'm1'})
    registry = _registry(2)
    registry['services']['e1']['config']['junk'] = {1, 2}
    out = asyncio.run(inventory.models(registry))
    assert [item['id'] for item in out] == ['m0']
    assert [problem['engine_id'] for problem in inventory.problems()] == ['e1']


# --- M-2 and L-1: a non-streamed chat answer is complete; numbers and embeddings are bounded -----

def _chat(stream=False, **over):
    message_key = 'delta' if stream else 'message'
    message = {'content': 'x'} if stream else {'role': 'assistant', 'content': 'x'}
    body = {'id': 'c1', 'object': 'chat.completion.chunk' if stream else 'chat.completion', 'created': 1, 'model': 'm',
            'choices': [{'index': 0, message_key: message, 'finish_reason': 'stop'}]}
    body.update(over)
    return body


def _null_finish(payload):
    payload['choices'][0]['finish_reason'] = None
    return payload


@pytest.mark.parametrize('label,payload,stream,intent', [
    ('control: finished chat', _chat(), False, 'chat'),
    ('control: stream chunk without a finish reason', _null_finish(_chat(stream=True)), True, 'chat'),
    ('control: usage-only stream chunk', _chat(stream=True, choices=[], usage={'prompt_tokens': 1, 'completion_tokens': 2}), True, 'chat'),
    ('control: embedding of floats', {'object': 'list', 'model': 'm', 'data': [{'object': 'embedding', 'index': 0, 'embedding': [0.5]}]}, False, 'embeddings'),
])
def test_M2_L1_accepted_shapes(label, payload, stream, intent):
    public_shape.validate(payload, intent, stream=stream)


@pytest.mark.parametrize('label,payload,stream,intent', [
    ('non-stream null finish_reason', _null_finish(_chat()), False, 'chat'),
    ('non-stream empty choices', _chat(choices=[]), False, 'chat'),
    ('integer token count beyond 2**53', _chat(usage={'prompt_tokens': 10 ** 30, 'completion_tokens': 1, 'total_tokens': 1}), False, 'chat'),
    ('empty base64 embedding', {'object': 'list', 'model': 'm', 'data': [{'object': 'embedding', 'index': 0, 'embedding': ''}]}, False, 'embeddings'),
])
def test_M2_L1_refused_shapes(label, payload, stream, intent):
    with pytest.raises(ProtocolError) as caught:
        public_shape.validate(payload, intent, stream=stream)
    assert caught.value.code == 'malformed_response'


def test_M2_ollama_done_without_a_reason_is_a_stop_on_the_public_wire(ollama):
    def handler(request):
        if request.url.path == '/api/chat':
            return httpx.Response(200, json={'model': 'llama3', 'message': {'role': 'assistant', 'content': 'Hi'}, 'done': True})
        raise AssertionError(request.url.path)
    client = ollama(handler)
    response = client.post('/v1/chat/completions', json={'model': 'llama3', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert response.status_code == 200, response.text
    assert response.json()['choices'][0]['finish_reason'] == 'stop'


def test_L1_refused_ollama_error_object_on_200_is_a_typed_failure(ollama):
    def handler(request):
        if request.url.path == '/api/chat':
            return httpx.Response(200, json={'error': 'model not found'})
        raise AssertionError(request.url.path)
    client = ollama(handler)
    response = client.post('/v1/chat/completions', json={'model': 'llama3', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert response.status_code == 502
    assert response.json()['error']['code'] == 'malformed_response'


@pytest.fixture
def ollama(monkeypatch, tmp_path):
    """A client for a native Ollama record, served by the test's own handler."""
    registry = {'schema_version': 2, 'services': {'ollama': {'id': 'ollama', 'enabled': True, 'config': {
        'connection': {'transport': 'http', 'dispatch': 'protocol', 'protocol': 'ollama-native',
                       'address': 'http://localhost:11434', 'credentials': {}},
        'offerings': [{'id': 'llama3', 'kind': 'model', 'upstream': 'llama3', 'tasks': [{'task': 'chat'}],
                       'selection': {'mode': 'request_field'}}]}}}}
    monkeypatch.setattr(proxy.ADMIN_STORE, 'get_service_registry', lambda: registry)
    monkeypatch.setattr(proxy, '_litellm_runtime_config', lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr(proxy, '_decision_models_for', lambda *a, **k: [])
    monkeypatch.setattr(media_jobs, 'DB_PATH', tmp_path / 'j.db')
    monkeypatch.setattr(receipt_store, 'DB_PATH', tmp_path / 'r.db')
    media_jobs.init_db(path=media_jobs.DB_PATH)
    receipt_store.init_db(path=receipt_store.DB_PATH)

    def client_for(handler):
        monkeypatch.setattr(executor, 'HttpxTransport', lambda: transport.HttpxTransport.for_tests(HandlerTransport(handler)))
        return TestClient(proxy.app)
    with authenticated():
        yield client_for


# --- L-3: a refused record echoed back with normalized defaults does not block an unrelated save --

REFUSED_STORED = {'id': 'paid', 'config': {'connection': dict(LOCAL, address='https://api.example.com'),
                                          'provider_type': 'remote'}}


def test_L3_echo_of_a_refused_record_with_a_default_field_does_not_block_an_unrelated_save():
    before = {'services': {'paid': copy.deepcopy(REFUSED_STORED)}}
    echoed = {'id': 'paid', 'enabled': True, 'config': copy.deepcopy(REFUSED_STORED['config'])}
    after = {'services': {'paid': echoed, 'other': record('other')}}
    sr.validate_protocol_dispatch_registry(after, before=before)


def test_L3_disable_only_repair_of_a_refused_record_still_passes():
    before = {'services': {'paid': copy.deepcopy(REFUSED_STORED)}}
    after = {'services': {'paid': dict(copy.deepcopy(REFUSED_STORED), enabled=False)}}
    sr.validate_protocol_dispatch_registry(after, before=before)


def test_L3_a_new_refused_record_is_still_refused():
    after = {'services': {'paid': copy.deepcopy(REFUSED_STORED)}}
    with pytest.raises(ValueError, match='paid'):
        sr.validate_protocol_dispatch_registry(after, before={'services': {}})


def test_L3_an_edit_that_adds_a_paid_marker_to_a_stored_local_record_is_refused():
    stored = record('e')
    edited = copy.deepcopy(stored)
    edited['config']['cost_class'] = 'paid-remote'
    with pytest.raises(ValueError):
        sr.validate_protocol_dispatch_registry({'services': {'e': edited}}, before={'services': {'e': stored}})


# --- L-2: an executor stream receipt records what a legacy stream receipt records ----------------

SSE = (b'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"coder","choices":[{"index":0,"delta":{"content":"a"},"finish_reason":null}]}\n\n'
       b'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"coder","choices":[{"index":0,"delta":{"content":"b"},"finish_reason":"stop"}]}\n\n'
       b'data: [DONE]\n\n')


def _receipt_projection(row):
    metadata = row['metadata']
    return {
        'status': row['status'],
        'stream': row.get('stream'),
        'terminal_reason': metadata.get('terminal_reason'),
        'stages': [stage['stage'] for stage in metadata.get('request_shape_stages', [])],
        'request_shape': sorted(metadata.get('request_shape', {})),
        'egress_status': [stage.get('status_code') for stage in metadata.get('request_shape_stages', [])
                          if stage['stage'] == 'backend_egress'],
    }


def test_L2_executor_stream_receipt_matches_the_legacy_receipt_for_the_same_request(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    fixture = json.loads((Path(__file__).parent / 'fixtures/engine_protocol/live-shaped.json').read_text())
    fixture['services']['exec'] = {'id': 'exec', 'enabled': True, 'config': {
        'connection': {'transport': 'http', 'dispatch': 'protocol', 'protocol': 'openai-compatible',
                       'address': 'http://127.0.0.1:18080', 'credentials': {}},
        'offerings': [{'id': 'exec-coder', 'kind': 'model', 'upstream': 'coder', 'tasks': [{'task': 'chat'}],
                       'selection': {'mode': 'request_field'}}]}}
    monkeypatch.setattr(proxy.ADMIN_STORE, 'get_service_registry', lambda: fixture)
    monkeypatch.setattr(proxy, 'config', {**copy.deepcopy(proxy.config), 'model_aliases': {}, 'roles': {}, 'models': [], 'backends': {}})
    monkeypatch.setattr(proxy, '_litellm_runtime_config', lambda: SimpleNamespace(enabled=True, service_id='litellm', base_url='http://substrate.test:4100', api_key='fixture-key'))
    monkeypatch.setattr(proxy, '_service_availability_response', lambda *a, **k: None)
    monkeypatch.setattr(proxy, '_prepare_litellm_local_llm_request', AsyncMock(side_effect=lambda *a, **kw: (None, kw['body_json'])))
    monkeypatch.setattr(proxy, '_activate_prepared_litellm_local_target', AsyncMock(return_value=None))

    async def legacy_send(request, **kwargs):
        return httpx.AsyncClient(), httpx.Response(200, content=SSE, headers={'content-type': 'text/event-stream'})
    monkeypatch.setattr(proxy, 'send_llm_execution_request', legacy_send)
    monkeypatch.setattr(executor, 'HttpxTransport', lambda: transport.HttpxTransport.for_tests(
        HandlerTransport(lambda request: httpx.Response(200, content=SSE, headers={'content-type': 'text/event-stream'}))))
    # The legacy route writes receipts through the store's default path, so use that path (sandboxed by the runner).
    monkeypatch.setattr(media_jobs, 'DB_PATH', tmp_path / 'j.db')
    media_jobs.init_db(path=media_jobs.DB_PATH)
    receipt_store.init_db()
    message = [{'role': 'user', 'content': 'hi'}]
    with authenticated():
        client = TestClient(proxy.app)
        legacy = client.post('/v1/chat/completions', json={'model': 'coder', 'messages': message, 'stream': True})
        protocol = client.post('/v1/chat/completions', json={'model': 'exec-coder', 'messages': message, 'stream': True})
    assert legacy.status_code == 200 and protocol.status_code == 200, (legacy.text, protocol.text)
    rows = {row['request_id']: row for row in receipt_store.recent()}
    legacy_row = rows[legacy.headers['x-azizam-request-id']]
    protocol_row = rows[protocol.headers['x-request-id']]
    assert _receipt_projection(protocol_row) == _receipt_projection(legacy_row)


# --- Probe rows kept as regressions: stream and embedding shapes, and inventory isolation ---------

def _stream_chunk(**over):
    chunk = {'id': 'c1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'm',
             'choices': [{'index': 0, 'delta': {'content': 'x'}, 'finish_reason': None}]}
    chunk.update(over)
    return chunk


@pytest.mark.parametrize('label,payload,stream,intent', [
    ('200 error object on a complete answer', {'error': {'message': 'boom', 'type': 'x'}}, False, 'chat'),
    ('NaN in usage', json.loads('{"id":"c","object":"chat.completion","created":1,"model":"m","choices":[{"index":0,"message":{"role":"assistant","content":"x"},"finish_reason":"stop"}],"usage":{"prompt_tokens":NaN,"completion_tokens":1,"total_tokens":1}}'), False, 'chat'),
    ('stream chunk without a finish key', _stream_chunk(choices=[{'index': 0, 'delta': {'content': 'a'}}]), True, 'chat'),
    ('stream content that is a number', _stream_chunk(choices=[{'index': 0, 'delta': {'content': 1}, 'finish_reason': None}]), True, 'chat'),
    ('stream error event', {'error': {'message': 'mid-stream'}}, True, 'chat'),
    ('stream NaN created', json.dumps({'id': 'c', 'object': 'chat.completion.chunk', 'created': float('nan'), 'model': 'm', 'choices': []}).encode(), True, 'chat'),
    ('embedding NaN', {'object': 'list', 'model': 'm', 'data': [{'object': 'embedding', 'index': 0, 'embedding': [float('nan')]}]}, False, 'embeddings'),
    ('embedding beyond float range', {'object': 'list', 'model': 'm', 'data': [{'object': 'embedding', 'index': 0, 'embedding': [10 ** 400]}]}, False, 'embeddings'),
    ('embedding with a boolean', {'object': 'list', 'model': 'm', 'data': [{'object': 'embedding', 'index': 0, 'embedding': [True]}]}, False, 'embeddings'),
    ('embedding base64 of a partial float', {'object': 'list', 'model': 'm', 'data': [{'object': 'embedding', 'index': 0, 'embedding': 'AAAAA'}]}, False, 'embeddings'),
])
def test_probe_rows_shape_refusals_stay_refused(label, payload, stream, intent):
    with pytest.raises(ProtocolError):
        public_shape.validate(payload, intent, stream=stream)


def _registry_of(n):
    return {'services': {f'e{i}': record(f'e{i}', config={'offerings': [offering(i)]}) for i in range(n)}}


def test_probe_forty_hanging_engines_share_one_listing_budget(monkeypatch, fresh_inventory, ollama_doc):
    monkeypatch.setattr(description, 'load_registry', lambda directory=None: {'ollama-native': ollama_doc})

    async def hang(self, op, body):
        await asyncio.sleep(3600)
    monkeypatch.setattr(dispatch.Runtime, 'invoke', hang)

    async def timed():
        started = time.perf_counter()
        out = await inventory.models(_registry_of(40))
        return time.perf_counter() - started, out
    elapsed, out = asyncio.run(timed())
    assert out == []
    assert elapsed < inventory.LIST_TIMEOUT_S + 0.5, elapsed


def test_probe_one_bad_engine_shape_is_isolated(monkeypatch, fresh_inventory, ollama_doc):
    monkeypatch.setattr(description, 'load_registry', lambda directory=None: {'ollama-native': ollama_doc})

    async def invoke(self, op, body):
        if self.target.engine_id == 'e1':
            return SimpleNamespace(body={'data': [{'id': 5}]}, asset=None)
        return SimpleNamespace(body={'data': [{'id': f'm{self.target.engine_id[1:]}'}]}, asset=None)
    monkeypatch.setattr(dispatch.Runtime, 'invoke', invoke)
    out = asyncio.run(inventory.models(_registry_of(3)))
    assert [item['id'] for item in out] == ['m0', 'm2']
    assert [problem['engine_id'] for problem in inventory.problems()] == ['e1']


def test_probe_engine_recovers_after_an_outage_and_the_cache_expires(monkeypatch, fresh_inventory, ollama_doc):
    monkeypatch.setattr(description, 'load_registry', lambda directory=None: {'ollama-native': ollama_doc})
    calls = {'down': False, 'n': 0}

    async def invoke(self, op, body):
        calls['n'] += 1
        if calls['down']:
            raise httpx.ConnectError('refused')
        return SimpleNamespace(body={'data': [{'id': 'm0'}]}, asset=None)
    monkeypatch.setattr(dispatch.Runtime, 'invoke', invoke)
    registry = _registry_of(1)
    asyncio.run(inventory.models(registry))
    asyncio.run(inventory.models(registry))
    assert calls['n'] == 1, 'a second listing within the TTL reuses the first'
    calls['down'] = True
    monkeypatch.setattr(inventory, 'CACHE_TTL_S', 0.0)
    assert asyncio.run(inventory.models(registry)) == []
    assert inventory.problems()
    calls['down'] = False
    assert [item['id'] for item in asyncio.run(inventory.models(registry))] == ['m0']
    assert inventory.problems() == []

"""W6a round-3 regressions: D24 refusal at every level, description load budget, response shape, registry round trip."""
import asyncio
import copy
import json
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def _control_listing_deadline(monkeypatch, started, expected, all_started):
    state = {'scheduled': [], 'fired': False}

    class ControlledAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def wait(self, tasks, timeout=None, return_when=asyncio.ALL_COMPLETED):
            state['scheduled'].append(timeout)
            await all_started.wait()
            assert len(started) == expected
            state['fired'] = True
            return set(), set(tasks)

    monkeypatch.setattr(inventory, 'time', SimpleNamespace(monotonic=lambda: 0.0))
    monkeypatch.setattr(inventory, 'asyncio', ControlledAsyncio())
    return state


def _control_description_deadline(monkeypatch, load_started):
    state = {'scheduled': [], 'fired': False}

    class ControlledAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def wait_for(self, awaitable, timeout):
            state['scheduled'].append(timeout)
            task = asyncio.ensure_future(awaitable)
            await load_started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            state['fired'] = True
            raise asyncio.TimeoutError

    monkeypatch.setattr(inventory, 'asyncio', ControlledAsyncio())
    return state


def test_M1_slow_description_load_cannot_outlive_the_listing_deadline(monkeypatch, fresh_inventory):
    loop = None
    load_started = asyncio.Event()
    load_finished = asyncio.Event()
    release_load = threading.Event()
    def slow(directory=None):
        loop.call_soon_threadsafe(load_started.set)
        try:
            release_load.wait()
            return {}
        finally:
            loop.call_soon_threadsafe(load_finished.set)
    monkeypatch.setattr(description, 'load_registry', slow)
    deadline = _control_description_deadline(monkeypatch, load_started)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(inventory.models(_registry(1)))
        try:
            out = await task
            assert not load_finished.is_set()
            return out
        finally:
            release_load.set()
            if load_started.is_set():
                await load_finished.wait()
    out = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert out == []
    assert deadline['scheduled'] == [inventory.DESCRIPTION_LOAD_S]
    assert deadline['fired']


def test_M1_timed_out_reload_serves_the_last_good_description(monkeypatch, fresh_inventory, ollama_doc):
    monkeypatch.setattr(description, 'load_registry', lambda directory=None: {'ollama-native': ollama_doc})
    _invoke_ids(monkeypatch, {'e0': 'm0'})
    asyncio.run(inventory.models(_registry(1)))
    monkeypatch.setattr(inventory, 'CACHE_TTL_S', 0.0)

    loop = None
    load_started = asyncio.Event()
    load_finished = asyncio.Event()
    release_load = threading.Event()
    def slow(directory=None):
        loop.call_soon_threadsafe(load_started.set)
        try:
            release_load.wait()
            return {}
        finally:
            loop.call_soon_threadsafe(load_finished.set)
    monkeypatch.setattr(description, 'load_registry', slow)
    deadline = _control_description_deadline(monkeypatch, load_started)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(inventory.models(_registry(1)))
        try:
            out = await task
            assert not load_finished.is_set()
            return out
        finally:
            release_load.set()
            if load_started.is_set():
                await load_finished.wait()
    out = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert [item['id'] for item in out] == ['m0']
    assert deadline['scheduled'] == [inventory.DESCRIPTION_LOAD_S]
    assert deadline['fired']


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


def _stream_receipt_rows(monkeypatch, tmp_path):
    """Drive one stream chat through the legacy route and through an executor record; return both receipts."""
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
    return rows[legacy.headers['x-azizam-request-id']], rows[protocol.headers['x-request-id']]


def _stream_receipt_metadata_keys(monkeypatch, tmp_path):
    legacy_row, protocol_row = _stream_receipt_rows(monkeypatch, tmp_path)
    legacy_keys, protocol_keys = set(legacy_row['metadata']), set(protocol_row['metadata'])
    return {'executor_only': sorted(protocol_keys - legacy_keys), 'legacy_only': sorted(legacy_keys - protocol_keys)}


def test_L2_executor_stream_receipt_matches_the_legacy_receipt_for_the_same_request(monkeypatch, tmp_path):
    legacy_row, protocol_row = _stream_receipt_rows(monkeypatch, tmp_path)
    assert _receipt_projection(protocol_row) == _receipt_projection(legacy_row)


def test_L2_the_only_metadata_keys_that_differ_between_the_two_routes_are_the_documented_ones(monkeypatch, tmp_path):
    # Documented: the executor names the engine and offering it ran (ENGINE_PROTOCOL_EXECUTOR.md, receipts).
    # Both routes write execution_backend; only its value differs.
    keys = _stream_receipt_metadata_keys(monkeypatch, tmp_path)
    # The legacy route names its service as service_id, where the executor names the engine as engine_id.
    assert keys['executor_only'] == ['engine_id', 'offering_id', 'upstream_id']
    assert keys['legacy_only'] == ['service_id']


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
    started = set()
    cancelled = set()
    all_started = asyncio.Event()
    deadline = _control_listing_deadline(monkeypatch, started, 40, all_started)
    async def in_thread(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(inventory.asyncio, 'to_thread', in_thread)

    async def hang(self, op, body):
        engine_id = self.target.engine_id
        started.add(engine_id)
        if len(started) == 40:
            all_started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.add(engine_id)
    monkeypatch.setattr(dispatch.Runtime, 'invoke', hang)

    out = asyncio.run(asyncio.wait_for(inventory.models(_registry_of(40)), timeout=30))
    assert out == []
    expected = {f'e{i}' for i in range(40)}
    assert started == expected
    assert cancelled == expected
    assert {problem['engine_id'] for problem in inventory.problems()} == expected
    assert all('total time limit' in problem['reason'] for problem in inventory.problems())
    assert deadline['scheduled'] == [inventory.LIST_TIMEOUT_S]
    assert deadline['fired']


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


# Remote capability factory gates: all callers use production adapter paths.
def test_remote_factory_main_dispatch(monkeypatch):
    from tests.support.remote_protocol import exercise
    asyncio.run(exercise('main', monkeypatch))


def test_remote_factory_fresh_observation(monkeypatch):
    from tests.support.remote_protocol import exercise
    asyncio.run(exercise('observation', monkeypatch))


def test_remote_factory_inventory(monkeypatch):
    from tests.support.remote_protocol import exercise
    asyncio.run(exercise('inventory', monkeypatch))


def test_remote_factory_control(monkeypatch):
    from tests.support.remote_protocol import exercise
    asyncio.run(exercise('control', monkeypatch))


def test_remote_factory_polling(monkeypatch):
    from tests.support.remote_protocol import exercise
    asyncio.run(exercise('polling', monkeypatch))


def test_remote_factory_assets(monkeypatch):
    from tests.support.remote_protocol import exercise
    asyncio.run(exercise('assets', monkeypatch))


def test_remote_binding_required_on_every_adapter_path():
    from tests.support.remote_protocol import case
    from gateway.engine_protocol.adapter import AdapterRegistry
    _, doc, _, cap, _, requests = case()
    with pytest.raises(ProtocolError):
        AdapterRegistry({doc.id: doc}, remote_capability=cap).adapter_for(doc.id)
    assert requests == []


def test_remote_same_origin_services_do_not_union_exceptions():
    from tests.support.remote_protocol import case
    registry, _, factory, cap, _, _ = case()
    other = copy.deepcopy(registry['services']['remote-box'])
    other['id'] = 'other-box'
    other['config']['connection']['remote_dispatch']['private_cidrs'] = ['10.0.0.0/24']
    registry['services']['other-box'] = other
    second = factory.admit('other-box')
    assert cap.address_exceptions == ()
    assert second.address_exceptions == ('10.0.0.0/24',)
    with pytest.raises(ProtocolError):
        factory.check(cap, service_id='other-box', address='https://engine.example')


def test_remote_policy_generation_rechecked_on_dispatch_and_reuse():
    from tests.support.remote_protocol import case
    registry, _, factory, cap, runtime, requests = case()
    registry['services']['remote-box']['config']['connection']['remote_dispatch']['private_cidrs'] = ['10.0.0.0/24']
    with pytest.raises(ProtocolError):
        factory.check(cap, service_id='remote-box', address='https://engine.example')
    with pytest.raises(ProtocolError):
        asyncio.run(runtime.invoke('state', {}))
    assert requests == []


def test_remote_policy_rejects_unapproved_records_and_redirects():
    from tests.support.remote_protocol import case
    from gateway.remote_engine import policy
    from gateway.engine_protocol.errors import RedirectRefused

    registry, _, factory, cap, runtime, requests = case()
    service = registry['services']['remote-box']
    unknown_billing = copy.deepcopy(service)
    unknown_billing['config'].pop('cost_class')
    with pytest.raises(ProtocolError):
        policy(unknown_billing)
    for override in ({'version': 2}, {'enabled': False}, {'enabled': 'true'}, {'unknown': True},
                     {'private_cidrs': ['10.0.0.0/8']}, {'private_cidrs': ['127.0.0.0/24']},
                     {'limits': {'polls': 301}}, {'limits': {'polls': 1.5}},
                     {'limits': {'total_s': float('nan')}}, {'limits': {'wire_bytes': True}},
                     {'limits': {'wire_bytes': 10 ** 1000}}):
        bad = copy.deepcopy(service)
        bad['config']['connection']['remote_dispatch'].update(override)
        with pytest.raises(ProtocolError):
            policy(bad)
    for level in ('record', 'config', 'connection', 'offering'):
        for key, value in [('paid_remote', True), ('provider_type', 'unknown'), ('cost_class', 'paid-remote'),
                           ('provider_type', False), ('cost_class', None)]:
            bad = copy.deepcopy(service)
            target = {'record': bad, 'config': bad['config'], 'connection': bad['config']['connection'],
                      'offering': bad['config']['offerings'][0]}[level]
            target[key] = value
            with pytest.raises(ProtocolError):
                policy(bad)
    bad = copy.deepcopy(runtime.target.service)
    bad['config']['connection']['credentials'] = {'inference': {'secret_ref': 'synthetic-reference', 'origin': 'https://other.example'}}
    with pytest.raises(ProtocolError):
        dispatch.Runtime(dispatch.Target('remote-box', bad, runtime.target.offering, runtime.target.description),
                         remote_factory=factory, remote_capability=cap)
    sent = []
    def redirect(request):
        sent.append(request)
        return httpx.Response(302, headers={'Location': '/another'})
    runtime._transport.inner = transport.HttpxTransport.for_tests(HandlerTransport(redirect))
    with pytest.raises(RedirectRefused):
        asyncio.run(runtime.invoke('state', {}))
    assert len(sent) == 1


def test_remote_observation_denied_dns_zero_connections(monkeypatch):
    from tests.support.remote_protocol import socket_case, fresh_observation
    from gateway.engine_switch import SwitchUnavailable
    async def check():
        for answers in (['127.0.0.1'], ['::1'], ['::ffff:127.0.0.1'], ['169.254.169.254'],
                        ['fd00:ec2::254'], ['100.100.100.200'], ['168.63.129.16'], ['0.0.0.0'],
                        ['224.0.0.1'], ['192.0.2.1'], ['100.64.0.1'], ['10.0.0.1'],
                        ['93.184.216.34', '127.0.0.1']):
            _, factory, _, runtime, recorder, _ = socket_case(monkeypatch, answers)
            with pytest.raises(ProtocolError):
                await runtime.invoke('state', {})
            assert recorder.connections == []
            with pytest.raises((ProtocolError, SwitchUnavailable)):
                await fresh_observation(runtime, factory)
            assert recorder.connections == []
        for answer, cidr, allowed in [
                ('10.1.2.3', '10.1.2.0/24', True), ('::ffff:10.1.2.3', '10.1.2.0/24', True),
                ('fd12:3456:789a:1::1', 'fd12:3456:789a:1::/64', True), ('fd00:ec2::254', 'fd00:ec2::/64', False)]:
            registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch, [answer])
            registry['services']['remote-box']['config']['connection']['remote_dispatch']['private_cidrs'] = [cidr]
            runtime = dispatch.Runtime(runtime.target, remote_factory=factory, remote_capability=factory.admit('remote-box'))
            if allowed:
                await runtime.invoke('state', {})
                assert len(recorder.connections) == 1
            else:
                with pytest.raises(ProtocolError):
                    await runtime.invoke('state', {})
                assert recorder.connections == []
    asyncio.run(check())


def test_remote_observation_redirect_zero_forbidden_connections(monkeypatch):
    from tests.support.remote_protocol import socket_case, fresh_observation
    from gateway.engine_switch import SwitchUnavailable
    async def check():
        for location in (b'https://169.254.169.254/', b'/same-origin'):
            _, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
            recorder.responses.append(b'HTTP/1.1 302 Found\r\nLocation: ' + location + b'\r\nContent-Length: 0\r\n\r\n')
            with pytest.raises(ProtocolError):
                await runtime.invoke('state', {})
            assert recorder.connections == [('93.184.216.34', 443)]
            assert len([w for w in recorder.writes if w.startswith(b'GET ')]) == 1
            recorder.responses.append(b'HTTP/1.1 302 Found\r\nLocation: ' + location + b'\r\nContent-Length: 0\r\n\r\n')
            with pytest.raises((ProtocolError, SwitchUnavailable)):
                await fresh_observation(runtime, factory)
            assert all(peer == ('93.184.216.34', 443) for peer in recorder.connections)
    asyncio.run(check())


def test_remote_observation_proxy_env_zero_forbidden_connections(monkeypatch):
    from tests.support.remote_protocol import socket_case
    monkeypatch.setenv('HTTPS_PROXY', 'http://169.254.169.254:8888')
    monkeypatch.setenv('ALL_PROXY', 'http://127.0.0.1:9999')
    async def check():
        _, _, _, runtime, recorder, _ = socket_case(monkeypatch)
        await runtime.invoke('state', {})
        assert recorder.connections == [('93.184.216.34', 443)]
    asyncio.run(check())


def test_remote_observation_credential_canary_zero_forbidden_connections(monkeypatch, caplog):
    from tests.support.remote_protocol import canary_path
    asyncio.run(canary_path('observation', monkeypatch, caplog))


def test_remote_poll_secret_canary_absent(monkeypatch, caplog):
    from tests.support.remote_protocol import canary_path
    asyncio.run(canary_path('poll', monkeypatch, caplog))


def test_remote_asset_secret_canary_absent(monkeypatch, caplog):
    from tests.support.remote_protocol import canary_path
    asyncio.run(canary_path('asset', monkeypatch, caplog))


def test_remote_control_secret_canary_absent(monkeypatch, caplog):
    from tests.support.remote_protocol import canary_path
    asyncio.run(canary_path('control', monkeypatch, caplog))


def test_remote_probe_secret_canary_absent(monkeypatch, caplog):
    from tests.support.remote_protocol import canary_path, socket_case
    asyncio.run(canary_path('probe', monkeypatch, caplog))
    async def missing():
        registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
        monkeypatch.delenv('REMOTE_TEST_MISSING_CREDENTIAL', raising=False)
        registry['services']['remote-box']['config']['connection']['credentials'] = {'control': {
            'secret_ref': 'os.environ/REMOTE_TEST_MISSING_CREDENTIAL', 'origin': 'https://engine.example'}}
        runtime = dispatch.Runtime(runtime.target, remote_factory=factory, remote_capability=factory.admit('remote-box'))
        from gateway.engine_protocol.types import CanonicalRequest
        await runtime.adapter.invoke(runtime.connection_for('state', probe=True), 'state', CanonicalRequest('state'))
        assert b'authorization:' not in b''.join(recorder.writes).lower()
        with pytest.raises(ProtocolError):
            await runtime.invoke('state', {})
        assert len(recorder.connections) == 1
    asyncio.run(missing())


def test_remote_dns_pinning_reconnect_and_tls_identity(monkeypatch, tmp_path):
    import httpcore
    real_connect = httpcore.AnyIOBackend.connect_tcp
    from tests.support.remote_protocol import socket_case
    async def check():
        _, _, _, runtime, recorder, answers = socket_case(monkeypatch)
        recorder.on_connect = lambda *_: answers.__setitem__(slice(None), ['127.0.0.1'])
        await runtime.invoke('state', {})
        assert recorder.connections == [('93.184.216.34', 443)]
        assert recorder.tls == ['engine.example']
        answers[:] = ['127.0.0.1']
        with pytest.raises(ProtocolError):
            await runtime.invoke('state', {})
        assert recorder.connections == [('93.184.216.34', 443)]
    asyncio.run(check())
    # Restore the OS backend before exercising real certificate verification.
    monkeypatch.setattr(httpcore.AnyIOBackend, 'connect_tcp', real_connect)
    from tests.support.remote_protocol import real_tls_identity
    asyncio.run(real_tls_identity(monkeypatch, tmp_path))


def test_remote_opt_out_closes_pool_and_cancels_queued_poll(monkeypatch, tmp_path):
    from tests.support.remote_protocol import socket_case
    from gateway.engine_protocol.transport import WireRequest
    async def check():
        registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
        started = asyncio.Event()
        async def worker():
            async with runtime._transport.operation():
                await runtime._transport.send(WireRequest('GET', 'https://engine.example/state', {}), runtime.connection.timeouts)
                started.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(worker())
        await started.wait()
        registry['services']['remote-box']['config']['connection']['remote_dispatch']['enabled'] = False
        factory.invalidate()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert recorder.closed
        assert len(recorder.connections) == 1
        with pytest.raises(ProtocolError):
            await runtime.invoke('state', {})
        assert len(recorder.connections) == 1
    asyncio.run(check())
    async def fence():
        import sqlite3
        registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
        database = tmp_path / 'fence.db'
        with sqlite3.connect(database) as conn:
            conn.execute('CREATE TABLE owner_policy (enabled INTEGER)')
        factory.db_path = database
        checks = []
        def owner_write():
            with sqlite3.connect(database, timeout=0) as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('INSERT INTO owner_policy VALUES (1)')
            checks.append(True)
        recorder.on_write = owner_write
        await runtime.invoke('state', {})
        assert checks
        with sqlite3.connect(database, timeout=0) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO owner_policy VALUES (0)')
        registry['services']['remote-box']['config']['connection']['remote_dispatch']['enabled'] = False
        with pytest.raises(ProtocolError):
            await runtime.invoke('state', {})
        assert len(recorder.connections) == 1
    asyncio.run(fence())


def test_remote_aggregate_deadline_spans_retries_and_polling(monkeypatch):
    from tests.support.remote_protocol import socket_case
    from gateway.engine_protocol.types import JobRef
    async def check():
        registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
        now = [0.0]
        factory.clock = lambda: now[0]
        registry['services']['remote-box']['config']['connection']['remote_dispatch']['limits'] = {'total_s': 1, 'polls': 1}
        cap = factory.admit('remote-box')
        runtime = dispatch.Runtime(runtime.target, remote_factory=factory, remote_capability=cap)
        await runtime.adapter.poll(runtime.connection_for('smell.infer'), JobRef('smell.infer', 'j'))
        with pytest.raises(ProtocolError):
            await runtime.adapter.poll(runtime.connection_for('smell.infer'), JobRef('smell.infer', 'j'))
        assert len(recorder.connections) == 1
        now[0] = 2.0
        with pytest.raises(ProtocolError):
            await runtime.invoke('state', {})
        assert len(recorder.connections) == 1
    asyncio.run(check())


def test_remote_wire_and_decompressed_byte_caps(monkeypatch):
    from tests.support.remote_protocol import socket_case
    async def check():
        for limits in ({'wire_bytes': 16}, {'decoded_bytes': 8}):
            registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
            registry['services']['remote-box']['config']['connection']['remote_dispatch']['limits'] = limits
            cap = factory.admit('remote-box')
            runtime = dispatch.Runtime(runtime.target, remote_factory=factory, remote_capability=cap)
            with pytest.raises(ProtocolError):
                await runtime.invoke('state', {})
            assert recorder.closed
        registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
        registry['services']['remote-box']['config']['connection']['remote_dispatch']['limits'] = {'decoded_bytes': 40}
        runtime = dispatch.Runtime(runtime.target, remote_factory=factory, remote_capability=factory.admit('remote-box'))
        await runtime.invoke('state', {})
        with pytest.raises(ProtocolError):
            await runtime.invoke('state', {})
        assert len(recorder.connections) == 2 and len(recorder.closed) >= 2
        import gzip
        _, _, _, runtime, recorder, _ = socket_case(monkeypatch)
        compressed = gzip.compress(b'x' * 100000)
        recorder.responses.append(b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: ' +
                                  str(len(compressed)).encode() + b'\r\n\r\n' + compressed)
        with pytest.raises(ProtocolError):
            await runtime.invoke('state', {})
        assert recorder.closed  # encoded bodies are refused before decompression
    asyncio.run(check())


def test_remote_header_limits_and_cancellation_close_socket(monkeypatch, tmp_path):
    from tests.support.remote_protocol import socket_case
    async def check():
        _, _, _, runtime, recorder, _ = socket_case(monkeypatch)
        recorder.responses.append(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX-Trailer: refused\r\n\r\n')
        with pytest.raises(ProtocolError):
            await runtime.invoke('state', {})
        assert recorder.closed
        for limits in ({'header_bytes': 32}, {'header_fields': 1}):
            registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
            registry['services']['remote-box']['config']['connection']['remote_dispatch']['limits'] = limits
            runtime = dispatch.Runtime(runtime.target, remote_factory=factory, remote_capability=factory.admit('remote-box'))
            with pytest.raises(ProtocolError):
                await runtime.invoke('state', {})
            assert recorder.closed
        _, _, _, runtime, recorder, _ = socket_case(monkeypatch)
        recorder.block_read = asyncio.Event()
        task = asyncio.create_task(runtime.invoke('state', {}))
        while not recorder.connections:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert recorder.closed
        registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
        registry['services']['remote-box']['config']['connection']['remote_dispatch']['limits'] = {'total_s': 0.03}
        runtime = dispatch.Runtime(runtime.target, remote_factory=factory, remote_capability=factory.admit('remote-box'))
        recorder.read_delay, recorder.fragment_size = 0.005, 1
        with pytest.raises((ProtocolError, TimeoutError, asyncio.CancelledError)):
            await runtime.invoke('state', {})
        assert recorder.closed
    asyncio.run(check())
    async def capacity():
        from gateway.remote_engine import ClientFactory
        from gateway.engine_protocol.transport import WireRequest
        registry, factory, _, runtime, recorder, _ = socket_case(monkeypatch)
        # Two independent factories for one service must share the connection ceiling.
        other_factory = ClientFactory(lambda: registry, connector=factory._connector)
        # The test directory is supplied by pytest, never the host's scratch defaults.
        factory.db_path = other_factory.db_path = tmp_path / 'capacity.db'
        other = dispatch.Runtime(runtime.target, remote_factory=other_factory,
                                 remote_capability=other_factory.admit('remote-box'))
        wire = WireRequest('GET', 'https://engine.example/state', {})
        response = await runtime._transport.send(wire, runtime.connection.timeouts)
        waiting = asyncio.create_task(other._transport.send(wire, other.connection.timeouts))
        await asyncio.sleep(0)
        assert not waiting.done() and len(recorder.connections) == 1
        await response.aclose()
        second = await asyncio.wait_for(waiting, 2)
        await second.aclose()
        assert len(recorder.connections) == 2
    asyncio.run(capacity())

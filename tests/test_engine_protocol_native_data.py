import json, httpx, pytest
from pathlib import Path
import proxy
from fastapi.testclient import TestClient
from gateway import media_jobs, receipt_store
from gateway.engine_protocol import description, transport, executor
from tests.support.access import authenticated
from tests.support.fake_upstream import HandlerTransport
from tests.test_engine_protocol_executor import Chunks
from types import SimpleNamespace
D = Path(__file__).resolve().parent.parent / 'registry/protocols'


def handler(request):
    p = request.url.path
    if p == '/api/chat':
        b = json.loads(request.content)
        if b.get('stream'):
            return httpx.Response(200, stream=Chunks([b'{"model":"llama3","message":{"role":"assistant","content":"Hi"},"done":false}\n', b'{"model":"llama3","message":{"role":"assistant","content":""},"done":true,"done_reason":"stop"}\n']))
        return httpx.Response(200, json={'model': 'llama3', 'message': {'role': 'assistant', 'content': 'Hi'}, 'done': True, 'done_reason': 'stop', 'prompt_eval_count': 3, 'eval_count': 1})
    if p == '/api/tags':
        return httpx.Response(200, json={'models': [{'name': 'llama3', 'size': 1}]})
    if p == '/api/embed':
        return httpx.Response(200, json={'model': 'nomic', 'embeddings': [[0.1, 0.2]], 'prompt_eval_count': 2})
    raise AssertionError(p)


@pytest.fixture
def ol(monkeypatch, tmp_path):
    rec = {'id': 'ollama', 'enabled': True, 'config': {'connection': {'transport': 'http', 'dispatch': 'protocol', 'protocol': 'ollama-native', 'address': 'http://localhost:11434', 'credentials': {}},
           'offerings': [{'id': 'llama3', 'kind': 'model', 'upstream': 'llama3', 'tasks': [{'task': 'chat'}], 'selection': {'mode': 'request_field'}},
                         {'id': 'nomic', 'kind': 'model', 'upstream': 'nomic', 'tasks': [{'task': 'embeddings'}], 'selection': {'mode': 'request_field'}}]}}
    reg = {'schema_version': 2, 'services': {'ollama': rec}}
    monkeypatch.setattr(proxy.ADMIN_STORE, 'get_service_registry', lambda: reg)
    orig = description.load_registry
    monkeypatch.setattr(description, 'load_registry', lambda directory=D: orig(directory))
    monkeypatch.setattr(description, 'SCHEMA_PATH', description.SCHEMA_PATH)
    wire = transport.HttpxTransport.for_tests(HandlerTransport(handler))
    monkeypatch.setattr(executor, 'HttpxTransport', lambda: wire)
    monkeypatch.setattr(proxy, '_litellm_runtime_config', lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr(media_jobs, 'DB_PATH', tmp_path / 'j.db'); monkeypatch.setattr(receipt_store, 'DB_PATH', tmp_path / 'r.db')
    media_jobs.init_db(path=media_jobs.DB_PATH); receipt_store.init_db(path=receipt_store.DB_PATH)
    monkeypatch.setattr(proxy, '_decision_models_for', lambda *a, **k: [])
    with authenticated():
        yield TestClient(proxy.app)


def test_native_chat_and_stream(ol):
    body = {'model':'llama3','messages':[{'role':'user','content':'hi'}]}
    response = ol.post('/v1/chat/completions', json=body)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['choices'][0]['message'] == {'role':'assistant','content':'Hi'}
    assert value['choices'][0]['finish_reason'] == 'stop'
    assert value['id'] and isinstance(value['created'], int)
    streamed = ol.post('/v1/chat/completions', json={**body,'stream':True})
    assert streamed.status_code == 200, streamed.text
    blocks = streamed.content.split(b'\n\n')
    assert blocks[-2] == b'data: [DONE]'
    chunks = [json.loads(block[6:]) for block in blocks[:-2]]
    assert ''.join(chunk['choices'][0]['delta']['content'] or '' for chunk in chunks) == 'Hi'
    assert chunks[-1]['choices'][0]['finish_reason'] == 'stop'
    assert len({chunk['id'] for chunk in chunks}) == 1


def test_native_model_items_are_mapped(ol):
    response = ol.get('/v1/models')
    assert response.status_code == 200, response.text
    model = next(item for item in response.json()['data'] if item['id'] == 'llama3')
    assert model['object'] == 'model'


def test_native_embedding_arrays_are_wrapped_and_dimensions_apply(ol):
    response = ol.post('/v1/embeddings', json={'model':'nomic','input':'hi'})
    assert response.status_code == 200, response.text
    assert response.json() == {'object':'list','model':'nomic','data':[{'object':'embedding','index':0,'embedding':[.1,.2]}],
        'usage':{'prompt_tokens':2,'total_tokens':2}}
    truncated = ol.post('/v1/embeddings', json={'model':'nomic','input':'hi','dimensions':1})
    assert truncated.status_code == 200, truncated.text
    assert truncated.json()['data'][0]['embedding'] == [1.0]

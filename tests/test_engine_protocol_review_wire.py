import copy, json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
from fastapi.testclient import TestClient
import proxy
from tests.support.access import authenticated

FIX = Path(__file__).parent / 'fixtures/engine_protocol/live-shaped.json'


def test_rv_wire(monkeypatch, tmp_path):
    from gateway import receipt_store, media_jobs
    receipt_store.init_db()

    registry = json.loads(FIX.read_text())
    monkeypatch.setattr(proxy.ADMIN_STORE, 'get_service_registry', lambda: registry)
    monkeypatch.setattr(proxy, 'config', {**copy.deepcopy(proxy.config), 'model_aliases': {}, 'roles': {}, 'models': [], 'backends': {}})
    monkeypatch.setattr(proxy, '_litellm_runtime_config', lambda: SimpleNamespace(enabled=True, service_id='litellm', base_url='http://substrate.test:4100', api_key='fixture-key'))
    monkeypatch.setattr(proxy, '_service_availability_response', lambda *a, **k: None)
    monkeypatch.setattr(proxy, '_prepare_litellm_local_llm_request', AsyncMock(side_effect=lambda *a, **kw: (None, kw['body_json'])))
    monkeypatch.setattr(proxy, '_activate_prepared_litellm_local_target', AsyncMock(return_value=None))
    wire = []
    async def send(request, **kwargs):
        wire.append({'method': request.method, 'url': request.url, 'body': request.body.decode(), 'read_timeout': request.read_timeout,
                     'headers': {k: v for k, v in request.headers.items() if k.lower() not in {'x-azizam-request-id', 'x-litellm-metadata'}}})
        if b'"stream": true' in request.body:
            return httpx.AsyncClient(), httpx.Response(200, content=b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"a"}}]}\r\n\r\ndata: {"id":"x","choices":[{"index":0,"delta":{"content":"b"},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":2}}\n\ndata: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
        if request.url.endswith('embeddings'):
            return httpx.AsyncClient(), httpx.Response(200, json={'object': 'list', 'data': [{'object': 'embedding', 'index': 0, 'embedding': [0.1]}], 'usage': {'prompt_tokens': 1, 'total_tokens': 1}})
        return httpx.AsyncClient(), httpx.Response(200, json={'id': 'z', 'object': 'chat.completion', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
    monkeypatch.setattr(proxy, 'send_llm_execution_request', send)
    out = []
    cases = [
        ('/v1/chat/completions', {'model': 'coder', 'messages': [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'héllo ☃'}], 'temperature': 0.2, 'max_tokens': 64, 'stop': ['\n'], 'seed': 7}),
        ('/v1/chat/completions', {'model': 'rp', 'messages': [{'role': 'user', 'content': 'x'}], 'stream': True, 'stream_options': {'include_usage': True}, 'top_p': 0.9}),
        ('/v1/chat/completions', {'model': 'default-fast', 'messages': [{'role': 'user', 'content': 'tools'}], 'tools': [{'type': 'function', 'function': {'name': 'f', 'parameters': {'type': 'object'}}}], 'tool_choice': 'auto'}),
        ('/v1/embeddings', {'model': 'fixture-embedding', 'input': ['a', 'b'], 'encoding_format': 'float'}),
        ('/v1/chat/completions', {'model': 'no-such-model', 'messages': [{'role': 'user', 'content': 'x'}]}),
    ]
    with authenticated():
        client = TestClient(proxy.app)
        for path, body in cases:
            n = len(wire)
            r = client.post(path, json=body)
            out.append({'path': path, 'status': r.status_code, 'resp': r.content.decode(), 'ct': r.headers.get('content-type'), 'wire': wire[n:]})
        r = client.get('/v1/models'); out.append({'models': r.status_code, 'ids': sorted(m['id'] for m in r.json().get('data', []))})
    rows=receipt_store.recent()
    print('RVRECEIPT='+json.dumps([{k:v for k,v in r.items() if k not in ('request_id','queued_at_ms','started_at_ms','first_output_at_ms','finished_at_ms','accepted_at_ms','updated_at_ms','_stored_at','latency_ms')} for r in rows[:6]],sort_keys=True,default=str))
    print('RVWIRE=' + json.dumps(out, sort_keys=True))

    assert out == json.loads((Path(__file__).parent / 'fixtures/engine_protocol/review-wire-main.json').read_text())

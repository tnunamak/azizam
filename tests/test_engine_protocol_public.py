"""Data-only HTTP engines through the real public handlers, with fake upstream wire evidence."""
import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import media_jobs, receipt_store
from gateway.engine_protocol import description, transport, executor
from tests.support.access import authenticated, identity, plan_for
from tests.support.fake_upstream import HandlerTransport
from tests.support.protocol_upstream import ProtocolUpstream

FIXTURE = Path(__file__).parent / 'fixtures/engine_protocol/prism'


@pytest.fixture
def engine(monkeypatch, tmp_path):
    record = json.loads((FIXTURE/'engine.json').read_text())
    fake = ProtocolUpstream()
    registry = {'schema_version':2,'services':{record['id']:record}}
    monkeypatch.setattr(proxy.ADMIN_STORE, 'get_service_registry', lambda: registry)
    monkeypatch.setattr(description, 'REGISTRY_DIR', FIXTURE)
    original = description.load_registry
    monkeypatch.setattr(description, 'load_registry', lambda directory=FIXTURE: original(directory))
    wire = transport.HttpxTransport.for_tests(HandlerTransport(fake))
    monkeypatch.setattr(executor, 'HttpxTransport', lambda: wire)
    monkeypatch.setattr(proxy, '_litellm_runtime_config', lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr(proxy, 'config', {**proxy.config,'model_aliases':{},'backends':{},'roles':{},'models':[]})
    monkeypatch.setattr(proxy, 'IMAGE_CACHE_DIR', tmp_path/'assets')
    monkeypatch.setattr(media_jobs, 'DB_PATH', tmp_path/'jobs.db')
    monkeypatch.setattr(receipt_store, 'DB_PATH', tmp_path/'receipts.db')
    media_jobs.init_db(path=media_jobs.DB_PATH)
    receipt_store.init_db(path=receipt_store.DB_PATH)
    monkeypatch.setattr(proxy, '_decision_models_for', lambda *a, **k: [])
    with authenticated():
        yield SimpleNamespace(record=record, fake=fake, client=TestClient(proxy.app), registry=registry)


def chat(engine, **kw):
    return engine.client.post('/v1/chat/completions', json={'model':'prism-chat','messages':[{'role':'user','content':'Hello'}],'temperature':9,**kw})


def test_public_chat_maps_request_response_and_receipt(engine):
    response = chat(engine)
    assert response.status_code == 200, response.text
    assert response.json() == {'object':'chat.completion','id':'answer-1','model':'text-v2','choices':[{'index':0,'message':{'role':'assistant','content':'Hi'},'finish_reason':'stop'}],'usage':{'prompt_tokens':3,'completion_tokens':1}}
    [wire] = engine.fake.requests
    assert str(wire.url) == 'http://prism.test:9100/prefix/api/v3/converse'
    assert wire.content == b'{"engine":{"model":"text-v2"},"input":{"turns":[{"role":"user","content":"Hello"}]},"delivery":"once","sampling":{"t":2}}'
    assert wire.headers['x-region'] == 'test'
    assert response.headers['x-request-id']


def test_public_sse_is_openai_framed_with_final_done(engine):
    response = chat(engine, stream=True)
    assert response.status_code == 200, response.text
    assert response.content == b'data: {"object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\ndata: {"object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":""},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\ndata: [DONE]\n\n'


def test_public_embeddings(engine):
    response = engine.client.post('/v1/embeddings', json={'model':'prism-embed','input':['hello']})
    assert response.status_code == 200, response.text
    assert response.json() == {'object':'list','data':[{'object':'embedding','index':0,'embedding':[0.25,0.75]}],'model':'embed-v3'}
    assert engine.fake.requests[0].content == b'{"encoder":"embed-v3","texts":["hello"]}'


def test_public_models_invokes_the_description(engine):
    response = engine.client.get('/v1/models')
    assert response.status_code == 200, response.text
    assert 'prism-chat' in {r['id'] for r in response.json()['data']}
    assert [(r.method,r.url.path,r.content) for r in engine.fake.requests] == [('GET','/prefix/catalog',b'')]


@pytest.mark.parametrize('sync', [False,True])
def test_public_images_use_core_assets_and_jobs(engine, sync):
    if sync:
        doc = description.parse_description_text((FIXTURE/'prism.yml').read_text(), source='prism.yml')
        from dataclasses import replace
        doc = replace(doc, operations={**doc.operations,'images':doc.operations['images_sync']})
        from unittest.mock import patch
        with patch.object(description, 'load_registry', return_value={'prism':doc}):
            response = engine.client.post('/v1/images/generations', json={'model':'prism-image','prompt':'blue','response_format':'b64_json'})
    else:
        response = engine.client.post('/v1/images/generations', json={'model':'prism-image','prompt':'blue','response_format':'b64_json'})
    assert response.status_code == 200, response.text
    item = response.json()['data'][0]
    assert base64.b64decode(item['b64_json']) == engine.fake.image
    assert item['asset_id'].startswith('img_')
    jobs = media_jobs.recent(path=media_jobs.DB_PATH, audience='internal')
    assert len(jobs) == 1 and jobs[0]['status'] == 'succeeded'
    assert jobs[0]['assets'][0]['asset_id'] == item['asset_id']
    if not sync:
        assert [(r.method,r.url.path,r.content) for r in engine.fake.requests] == [('POST','/prefix/api/v3/renders',b'{"spec":{"text":"blue","dims":"512x512"}}'),('GET','/prefix/api/v3/renders/job-1',b''),('GET','/prefix/api/v3/renders/job-1/outputs/output-1',b'')]


def test_upstream_advice_becomes_typed_public_failure(engine):
    engine.fake.status=429
    response=chat(engine)
    assert (response.status_code,response.json()['error']['code']) == (429,'engine_busy')
    assert response.headers['retry-after'] == '7'


def test_public_speech_if_declared(engine):
    response=engine.client.post('/v1/audio/speech',json={'model':'prism-speech','input':'hi','voice':'one','response_format':'wav'})
    assert response.status_code == 200, response.text
    assert response.content[:4] == b'RIFF'
    assert engine.fake.requests[0].content == b'{"text":"hi","voice_model":"voice-v1"}'

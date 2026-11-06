"""Frozen wires made on r65 plus W4 before W6a, with no operator data."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import service_registry
from gateway.litellm_substrate import compile_litellm_config_from_registry
from tests.support.access import authenticated

FIXTURE = Path(__file__).parent/'fixtures/engine_protocol/live-shaped.json'
GOLDEN = Path(__file__).parent/'fixtures/engine_protocol/legacy-wire.json'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def snapshot(monkeypatch):
    registry=json.loads(FIXTURE.read_text())
    seed=service_registry.build_service_registry_seed(proxy.config)
    results={'projection':service_registry.runtime_backends(registry),
             'compiled':compile_litellm_config_from_registry(registry),
             'seeded_projection':service_registry.runtime_backends(seed),
             'seeded_compiled':compile_litellm_config_from_registry(seed),'wires':[]}
    monkeypatch.setattr(proxy.ADMIN_STORE,'get_service_registry',lambda:registry)
    monkeypatch.setattr(proxy,'config',{**copy.deepcopy(proxy.config),'model_aliases':{},'roles':{},'models':[], 'backends':{}})
    monkeypatch.setattr(proxy,'_litellm_runtime_config',lambda:SimpleNamespace(enabled=True,service_id='litellm',base_url='http://substrate.test:4100',api_key='fixture-key'))
    monkeypatch.setattr(proxy,'_service_availability_response',lambda *a,**k:None)
    monkeypatch.setattr(proxy,'_prepare_litellm_local_llm_request',AsyncMock(side_effect=lambda *a,**kw:(None,kw['body_json'])))
    monkeypatch.setattr(proxy,'_activate_prepared_litellm_local_target',AsyncMock(return_value=None))
    request_wire=[]
    async def send(request,**kwargs):
        request_wire.append({'method':request.method,'url':request.url,'body':request.body.decode(),
            'read_timeout':request.read_timeout,'headers':{k:v for k,v in request.headers.items() if k.lower() not in {'x-azizam-request-id','x-litellm-metadata'}}})
        payload={'object':'list','data':[]} if request.url.endswith('embeddings') else {'id':'legacy','object':'chat.completion','choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}]}
        if b'"stream": true' in request.body:
            return httpx.AsyncClient(),httpx.Response(200,content=b'data: {"id":"legacy","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',headers={'content-type':'text/event-stream'})
        return httpx.AsyncClient(),httpx.Response(200,json=payload)
    monkeypatch.setattr(proxy,'send_llm_execution_request',send)
    with authenticated():
        client=TestClient(proxy.app)
        for model in ['default','coder','coder-q5','coder-q8','default-fast','rp','fixture-embedding']:
            for stream in ([False,True] if model!='fixture-embedding' else [False]):
                path='/v1/embeddings' if model=='fixture-embedding' else '/v1/chat/completions'
                body={'model':model,'input':'hi'} if path.endswith('embeddings') else {'model':model,'messages':[{'role':'user','content':'hi'}],'stream':stream}
                response=client.post(path,json=body)
                assert response.status_code==200,response.text
                results['wires'].append({'request':request_wire[-1], 'response':response.content.decode(), 'content_type':response.headers.get('content-type')})
    return results


def test_every_seed_and_live_shaped_projection_compiler_and_public_wire_is_frozen(monkeypatch):
    result=snapshot(monkeypatch)
    # Optional artifact output is an internal-only tool for the old/new differential run.
    import os
    if os.environ.get('AZIZAM_UPDATE_GOLDEN')=='1':
        print('LEGACY_WIRE_JSON='+json.dumps(result,sort_keys=True,separators=(',',':')))
        return
    from gateway.litellm_content import profile
    # Check the intentional substrate additions, then compare every frozen wire.
    for name in ('compiled', 'seeded_compiled'):
        assert result[name].pop('router_settings') == profile()['router_settings']
        for section in ('general_settings', 'litellm_settings'):
            for key, value in profile()[section].items():
                assert result[name][section].pop(key) == value
    assert result==json.loads(GOLDEN.read_text())

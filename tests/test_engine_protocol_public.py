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
    monkeypatch.setattr(proxy, 'IMAGE_MEDIA_JOB_PAYLOAD_DIR', tmp_path/'payloads')
    original_record=receipt_store.record
    monkeypatch.setattr(receipt_store,'record',lambda receipt:original_record(receipt,path=receipt_store.DB_PATH))
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
    assert response.json() == {'object':'chat.completion','id':'answer-1','model':'text-v2','choices':[{'index':0,'message':{'role':'assistant','content':'Hi'},'finish_reason':'stop'}],'usage':{'prompt_tokens':3,'completion_tokens':1,'total_tokens':4},'created':1700000000}
    [wire] = engine.fake.requests
    assert str(wire.url) == 'http://localhost:9100/prefix/api/v3/converse'
    assert wire.content == b'{"engine":{"model":"text-v2"},"input":{"turns":[{"role":"user","content":"Hello"}]},"delivery":"once","sampling":{"t":2}}'
    assert wire.headers['x-region'] == 'test'
    assert response.headers['x-request-id']


def test_public_sse_is_openai_framed_with_final_done(engine):
    response = chat(engine, stream=True)
    assert response.status_code == 200, response.text
    expected = [
        {'object':'chat.completion.chunk','choices':[{'index':0,'delta':{'content':'Hi'},'finish_reason':None}],
         'id':'stream-1','created':1700000000,'model':'text-v2'},
        {'object':'chat.completion.chunk','choices':[{'index':0,'delta':{'content':''},'finish_reason':'stop'}],
         'usage':{'prompt_tokens':3,'completion_tokens':1},'id':'stream-1','created':1700000000,'model':'text-v2'},
    ]
    assert response.content == b''.join(b'data: '+json.dumps(event,separators=(',',':')).encode()+b'\n\n' for event in expected)+b'data: [DONE]\n\n'


def test_public_embeddings(engine):
    response = engine.client.post('/v1/embeddings', json={'model':'prism-embed','input':['hello']})
    assert response.status_code == 200, response.text
    assert response.json() == {'object':'list','data':[{'object':'embedding','index':0,'embedding':[0.25,0.75]}],'model':'embed-v3','usage':{'prompt_tokens':1,'total_tokens':1}}
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
    assert response.json()=={'created':jobs[0]['updated_at'],'job_id':jobs[0]['job_id'],'data':[{'asset_id':item['asset_id'],'media_type':'image/png','b64_json':base64.b64encode(engine.fake.image).decode()}]}
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


def switching(engine):
    cfg = engine.record['config']
    cfg['observations'] = {'selected':{'op':'selected'}}
    cfg['offerings'] = [cfg['offerings'][0], {**copy.deepcopy(cfg['offerings'][0]), 'id':'prism-old','upstream':'text-old'}]
    for offering in cfg['offerings']:
        offering['selection'] = {'mode':'switch','switch':{'call':'choose','args':{'model':offering['upstream']},'observe_selected':'selected','exclusive_scope':'engine','drain_timeout_s':0.02,'queue_wait_s':0.02,'on_failure':'restore'}}


def test_switch_mode_uses_declared_control_then_verifies_before_public_chat(engine):
    switching(engine)
    response = chat(engine)
    assert response.status_code == 200, response.text
    assert [(r.method,r.url.path,r.content) for r in engine.fake.requests] == [('GET','/prefix/selected',b''),('POST','/prefix/selected',b'{"model":"text-v2"}'),('GET','/prefix/selected',b''),('POST','/prefix/api/v3/converse',b'{"engine":{"model":"text-v2"},"input":{"turns":[{"role":"user","content":"Hello"}]},"delivery":"once","sampling":{"t":2}}')]


def test_unknown_offering_never_switches(engine):
    switching(engine)
    response=engine.client.post('/v1/chat/completions',json={'model':'prism-local/missing','messages':[]})
    assert response.status_code >= 400
    assert not engine.fake.requests


def test_unverified_switch_restores_and_never_generates(engine):
    switching(engine)
    engine.fake.fail_selection=True
    response=chat(engine)
    assert response.status_code == 503, response.text
    assert response.headers['retry-after']
    assert all(r.url.path != '/prefix/api/v3/converse' for r in engine.fake.requests)
    assert engine.fake.requests[-2].content == b'{"model":"text-old"}'


def test_switch_lease_holds_until_public_stream_finishes(engine,monkeypatch):
    import asyncio
    from gateway.swap_drain import SwapDrain
    switching(engine)
    async def run():
        drain=SwapDrain(); locks={}
        monkeypatch.setattr(proxy,'_swap_drain',drain)
        monkeypatch.setattr(proxy,'swap_lock_for',lambda key:locks.setdefault(key,asyncio.Lock()))
        entered=asyncio.Event();release=asyncio.Event()
        from tests.test_engine_protocol_executor import Chunks
        old=engine.fake.handler if hasattr(engine.fake,'handler') else engine.fake.__call__
        def handler(request):
            if request.url.path.endswith('/converse') and b'"live"' in request.content:
                engine.fake.requests.append(request)
                class Held(Chunks):
                    async def __aiter__(self):
                        yield b'data: {"delta":"Hi","done":false}\n\n'
                        entered.set()
                        await release.wait()
                        yield b'data: {"delta":"","done":true,"why":"eos","meter":{"in":3,"out":1}}\n\n'
                return httpx.Response(200,stream=Held([]),headers={'content-type':'text/event-stream'})
            return old(request)
        fake_wire=transport.HttpxTransport.for_tests(HandlerTransport(handler))
        monkeypatch.setattr(executor,'HttpxTransport',lambda:fake_wire)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app),base_url='http://gateway.test') as client:
            first=asyncio.create_task(client.post('/v1/chat/completions',json={'model':'prism-chat','messages':[],'stream':True}))
            await asyncio.wait_for(entered.wait(),2)
            second=await client.post('/v1/chat/completions',json={'model':'prism-old','messages':[]})
            assert second.status_code == 503, second.text
            assert engine.fake.active == 'text-v2'
            release.set()
            assert (await first).status_code == 200
            third=await client.post('/v1/chat/completions',json={'model':'prism-old','messages':[]})
            assert third.status_code == 200, third.text
            assert engine.fake.active == 'text-old'
    asyncio.run(run())


def test_credentials_use_origin_and_operation_class_without_forwarding_client_key(engine,monkeypatch):
    monkeypatch.setenv('PROTOCOL_INFERENCE','test-inference')
    monkeypatch.setenv('PROTOCOL_CONTROL','test-control')
    conn=engine.record['config']['connection']
    conn['credentials']={cls:{'secret_ref':'os.environ/PROTOCOL_'+cls.upper(),'origin':'http://localhost:9100','header':header} for cls,header in [('inference','X-Inference'),('control','X-Control')]}
    switching(engine)
    response=chat(engine)
    assert response.status_code == 200,response.text
    assert engine.fake.requests[-1].headers['x-inference']=='test-inference'
    assert 'x-control' not in engine.fake.requests[-1].headers
    assert all(r.headers['x-control']=='test-control' and 'x-inference' not in r.headers for r in engine.fake.requests[:-1])
    engine.fake.requests.clear()
    conn['credentials']['inference']['origin']='http://different.test:9100'
    response=chat(engine)
    assert response.status_code == 502
    assert not engine.fake.requests


def test_image_idempotency_replays_without_resubmit_and_conflicts_on_changed_inputs(engine):
    body={'model':'prism-image','prompt':'blue'}
    first=engine.client.post('/v1/images/generations',json=body,headers={'idempotency-key':'one'})
    assert first.status_code==200,first.text
    count=len(engine.fake.requests)
    second=engine.client.post('/v1/images/generations',json=body,headers={'idempotency-key':'one'})
    assert second.status_code==200,second.text
    assert second.json()['job_id']==first.json()['job_id']
    assert len(engine.fake.requests)==count
    changed=engine.client.post('/v1/images/generations',json={**body,'prompt':'green'},headers={'idempotency-key':'one'})
    assert changed.status_code==409,changed.text
    assert len(engine.fake.requests)==count



def test_admission_denial_precedes_all_protocol_network_calls(engine,monkeypatch):
    monkeypatch.setattr(proxy,'_effective_media_job_admission_cfg',lambda:{'disabled_intents':['chat.generate']})
    response=chat(engine)
    assert response.status_code==503,response.text
    assert not engine.fake.requests


def test_plan_denial_precedes_all_protocol_network_calls(engine):
    with authenticated(identity(plan_for(caps=('speech',)))):
        response=chat(engine)
    assert response.status_code==403,response.text
    assert not engine.fake.requests


def test_no_switch_permission_is_checked_after_acquiring_selection_lock(engine):
    import asyncio
    from gateway.engine_protocol import dispatch
    from gateway.engine_switch import SwitchUnavailable
    from gateway.swap_drain import SwapDrain
    switching(engine)
    engine.fake.active='text-v2'
    class RacedLock:
        async def acquire(self):
            # Authorized B wins the lock while restricted A is queued.
            engine.fake.active='text-old'
        def release(self):
            pass
    async def go():
        runtime=dispatch.Runtime(dispatch.resolve(engine.registry,'prism-chat','chat'))
        try:
            lease=await runtime.lease(drain=SwapDrain(),lock_for=lambda _:RacedLock(),may_switch=False)
        except SwitchUnavailable:
            return
        lease.release()
        pytest.fail('Restricted caller switched after its pre-lock observation went stale')
    asyncio.run(go())
    assert not [r for r in engine.fake.requests if r.method=='POST']


def test_contract_http_observations_verify_selected_and_offering_ready(engine,monkeypatch):
    switching(engine)
    engine.record['config']['observations']={'selected':{'http':{'path':'/selected'},'select':'/active'},
        'offering_ready':{'http':{'path':'/ready','select':'/ready'}}}
    original=engine.fake.__call__
    def handler(request):
        if request.url.path=='/prefix/ready':
            engine.fake.requests.append(request)
            return httpx.Response(200,json={'ready':True})
        return original(request)
    wire=transport.HttpxTransport.for_tests(HandlerTransport(handler))
    monkeypatch.setattr(executor,'HttpxTransport',lambda:wire)
    response=chat(engine)
    assert response.status_code==200,response.text
    assert any(r.url.path=='/prefix/ready' for r in engine.fake.requests)


def test_async_submission_receives_the_same_offering_parameters_as_sync(engine,monkeypatch):
    import yaml
    data=yaml.safe_load((FIXTURE/'prism.yml').read_text())
    data['operations']['images']['job']['submit']['request']['query']=[{'name':'offering','from':'/params/offering'},{'name':'model','from':'/params/model'}]
    doc=description.parse_description(data,source='prism.yml')
    monkeypatch.setattr(description,'load_registry',lambda:{'prism':doc})
    response=engine.client.post('/v1/images/generations',json={'model':'prism-image','prompt':'blue'})
    assert response.status_code==200,response.text
    assert str(engine.fake.requests[0].url)=='http://localhost:9100/prefix/api/v3/renders?offering=prism-image&model=render'


@pytest.mark.parametrize('failure',['blocked',503])
def test_core_job_poll_keeps_typed_errors_and_retries_only_transient_failures(engine,monkeypatch,failure):
    from gateway.engine_protocol.errors import TransportFailure
    original=engine.fake.__call__; polls=[]
    def handler(request):
        if request.url.path=='/prefix/api/v3/renders/job-1' and request.method=='GET':
            polls.append(1)
            if failure=='blocked':
                raise TransportFailure('blocked','Destination refused')
            if len(polls)==1:
                return httpx.Response(503,text='temporarily busy')
        return original(request)
    wire=transport.HttpxTransport.for_tests(HandlerTransport(handler))
    monkeypatch.setattr(executor,'HttpxTransport',lambda:wire)
    response=engine.client.post('/v1/images/generations',json={'model':'prism-image','prompt':'blue'})
    if failure=='blocked':
        assert response.status_code==502,response.text
        assert response.json()['error']['code']=='transport_failed'
        assert polls==[1]
    else:
        assert response.status_code==200,response.text
        assert len(polls)==2


@pytest.mark.parametrize('op',['images','models'])
def test_malformed_upstream_collections_are_typed_failures(engine,monkeypatch,op):
    from dataclasses import replace
    import yaml
    doc=yaml.safe_load((FIXTURE/'prism.yml').read_text())
    name='images' if op=='images' else 'models'
    doc['operations'][name]={'class':'generate' if op=='images' else 'inventory','request':{'method':'GET','path':'/broken'},'response':{'framing':'json'}}
    parsed=description.parse_description(doc,source='prism.yml')
    monkeypatch.setattr(description,'load_registry',lambda:{'prism':parsed})
    wire=transport.HttpxTransport.for_tests(HandlerTransport(lambda r:httpx.Response(200,json={'data':[None]})))
    monkeypatch.setattr(executor,'HttpxTransport',lambda:wire)
    response=(engine.client.get('/v1/models') if op=='models' else engine.client.post('/v1/images/generations',json={'model':'prism-image','prompt':'blue'}))
    if op == 'models':
        assert response.status_code == 200, response.text
        from gateway.engine_protocol import inventory
        assert inventory.problems()
    else:
        assert response.status_code==502,response.text
        assert response.json()['error']['code']=='malformed_response'


@pytest.mark.parametrize('denial',['policy','admission'])
def test_protocol_owned_denials_leave_a_terminal_receipt(engine,monkeypatch,denial):
    import asyncio
    from starlette.requests import Request
    from tests.support.access import access_context
    auth=access_context(plan_for(caps=('speech',))) if denial=='policy' else access_context()
    if denial=='admission':
        monkeypatch.setattr(proxy,'_effective_media_job_admission_cfg',lambda:{'disabled_intents':['chat.generate']})
    request=Request({'type':'http','method':'POST','path':'/v1/chat/completions','headers':[], 'scheme':'http','server':('gateway.test',80),'query_string':b''})
    response=asyncio.run(proxy._try_protocol_request(request,auth,'chat',body={'model':'prism-chat','messages':[]}))
    assert response.status_code in {403,503},response.body
    rows=receipt_store.recent(path=receipt_store.DB_PATH)
    assert len(rows)==1 and rows[0]['status'] not in {'accepted','in_progress'},rows
    assert not engine.fake.requests


def test_failed_switch_is_a_typed_public_failure_with_terminal_receipt(engine):
    switching(engine);engine.fake.fail_selection=True
    response=chat(engine)
    assert response.status_code==503
    assert response.json()['error']['code']=='selection_unavailable'
    rows=receipt_store.recent(path=receipt_store.DB_PATH)
    assert len(rows)==1 and rows[0]['status']=='failed',rows


def test_core_job_cancel_propagates_to_the_existing_worker_settler(engine,monkeypatch):
    import asyncio
    from starlette.requests import Request
    from tests.support.access import access_context
    from gateway.engine_protocol import dispatch
    original=engine.fake.__call__;called=[]
    def handler(request):
        called.append((request.method,request.url.path))
        if request.method=='GET' and request.url.path=='/prefix/api/v3/renders/job-1':
            return httpx.Response(200,json={'phase':'WORKING'})
        return original(request)
    wire=transport.HttpxTransport.for_tests(HandlerTransport(handler))
    monkeypatch.setattr(executor,'HttpxTransport',lambda:wire)
    job=media_jobs.create(media_jobs.MediaJob(intent='image.generate',backend='prism-local'),path=media_jobs.DB_PATH)
    media_jobs.update_status(job.job_id,'running',metadata_patch={'protocol_job':'job-1'},path=media_jobs.DB_PATH)
    media_jobs.request_cancel(job.job_id,path=media_jobs.DB_PATH)
    request=Request({'type':'http','method':'POST','path':'/v1/images/generations','headers':[], 'scheme':'http','server':('gateway.test',80),'query_string':b''})
    auth=access_context();receipt=proxy.start_image_route_receipt('/v1/images/generations',auth);receipt.job_id=job.job_id
    async def go():
        token=proxy._ADMISSION_WORKER.set(True)
        try:
            with pytest.raises(media_jobs.CancellationPending):
                await proxy._try_protocol_request(request,auth,'images',body={'model':'prism-image','prompt':'blue'},receipt=receipt)
        finally:
            proxy._ADMISSION_WORKER.reset(token)
    asyncio.run(go())
    assert ('DELETE','/prefix/api/v3/renders/job-1') in called


def test_unmapped_multiline_sse_retains_every_data_line(engine,monkeypatch):
    import yaml
    from tests.test_engine_protocol_executor import Chunks
    data=yaml.safe_load((FIXTURE/'prism.yml').read_text())
    stream=data['operations']['chat']['response']['stream'];stream.pop('map');stream['terminal']={'sentinel':'[DONE]'}
    doc=description.parse_description(data,source='prism.yml')
    monkeypatch.setattr(description,'load_registry',lambda:{'prism':doc})
    raw=b'data: {"object":"chat.completion.chunk","id":"a","created":0,"model":"m","choices":\ndata: []}\n\ndata: [DONE]\n\n'
    wire=transport.HttpxTransport.for_tests(HandlerTransport(lambda r:httpx.Response(200,stream=Chunks([raw]))))
    monkeypatch.setattr(executor,'HttpxTransport',lambda:wire)
    response=chat(engine,stream=True)
    assert response.status_code==200,response.text
    assert response.content==raw


def test_queued_protocol_image_jobs_do_not_depend_on_other_engine_enablement(engine,monkeypatch):
    from unittest.mock import Mock
    job=media_jobs.create(media_jobs.MediaJob(intent='image.generate',backend='prism-local',metadata={'mode':'async_image_generation'}),path=media_jobs.DB_PATH)
    monkeypatch.setattr(proxy,'COMFY_ENABLED',False)
    monkeypatch.setattr(proxy,'IMAGE_MEDIA_JOB_RESUME_ON_START',True)
    monkeypatch.setattr(proxy,'_has_image_media_job_payload',lambda _:True)
    scheduled=Mock(return_value=True)
    monkeypatch.setattr(proxy,'_schedule_image_media_job',scheduled)
    assert proxy._resume_pending_image_media_jobs()==1
    scheduled.assert_called_once_with(job.job_id)


@pytest.mark.parametrize('cancel',[False,True])
def test_public_queued_protocol_image_runs_through_the_real_worker_and_settler(engine,monkeypatch,cancel):
    import asyncio
    monkeypatch.setattr(proxy,'_schedule_image_media_job',lambda _:False)
    monkeypatch.setattr(proxy,'IMAGE_MEDIA_JOB_MAX_ATTEMPTS',1)
    body={'model':'prism-image','prompt':'blue'}
    response=engine.client.post('/gateway/v1/media/jobs/image-generations',json=body)
    assert response.status_code==202,response.text
    job_id=response.json()['job_id']
    job=media_jobs.get(job_id,audience='internal',path=media_jobs.DB_PATH)
    assert job['backend']=='prism-local'
    original=engine.fake.__call__
    def handler(request):
        if cancel and request.method=='POST' and request.url.path=='/prefix/api/v3/renders':
            media_jobs.request_cancel(job_id,path=media_jobs.DB_PATH)
        if cancel and request.method=='GET' and request.url.path=='/prefix/api/v3/renders/job-1':
            return httpx.Response(200,json={'phase':'WORKING'})
        return original(request)
    wire=transport.HttpxTransport.for_tests(HandlerTransport(handler))
    monkeypatch.setattr(executor,'HttpxTransport',lambda:wire)
    asyncio.run(proxy._run_image_media_job(job_id))
    job=media_jobs.get(job_id,audience='internal',path=media_jobs.DB_PATH)
    assert job['status']==('canceled' if cancel else 'succeeded'),job.get('error')
    rows=receipt_store.recent(path=receipt_store.DB_PATH)
    assert any(row['status']==('cancelled' if cancel else 'ok') for row in rows),rows


def test_protocol_image_assets_keep_existing_owner_access_rules(engine):
    response=engine.client.post('/v1/images/generations',json={'model':'prism-image','prompt':'blue'})
    assert response.status_code==200,response.text
    asset_id=response.json()['data'][0]['asset_id']
    path=f'/v1/assets/{asset_id}/content'
    from tests.support.access import access_context
    assert engine.client.get(response.json()['data'][0]['url']).status_code==200
    assert proxy._asset_access_allowed(asset_id,access_context())
    assert not proxy._asset_access_allowed(asset_id,access_context(identity(app_id='different-app',key_hash='different-key').access,app_id='different-app',key_hash='different-key'))


def test_model_inventory_cannot_invoke_a_control_operation(engine,monkeypatch):
    import yaml
    data=yaml.safe_load((FIXTURE/'prism.yml').read_text())
    data['operations']['models']['class']='control'
    data['operations']['models']['request']={'method':'POST','path':'/selected','body':{'encoding':'json'}}
    doc=description.parse_description(data,source='prism.yml')
    monkeypatch.setattr(description,'load_registry',lambda:{'prism':doc})
    response=engine.client.get('/v1/models')
    assert response.status_code==200,response.text
    from gateway.engine_protocol import inventory
    assert inventory.problems()
    assert not engine.fake.requests


def test_fictional_chat_and_embeddings_data_include_required_openai_response_fields(engine):
    response=chat(engine)
    assert response.status_code==200,response.text
    assert isinstance(response.json()['created'],int)
    assert response.json()['usage']['total_tokens']==4
    streamed=chat(engine,stream=True)
    assert streamed.status_code==200
    for block in streamed.content.split(b'\n\n'):
        if block and block!=b'data: [DONE]':
            chunk=json.loads(block.removeprefix(b'data: '))
            assert {'id','created','model','object','choices'}<=chunk.keys()
    embedded=engine.client.post('/v1/embeddings',json={'model':'prism-embed','input':['hello']})
    assert embedded.json()['usage']=={'prompt_tokens':1,'total_tokens':1}

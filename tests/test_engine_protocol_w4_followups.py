"""Regression evidence for the reviewed W4 follow-ups before W6a uses jobs."""
import asyncio

import httpx
import pytest

from gateway.engine_protocol import description
from gateway.engine_protocol.errors import BudgetExceeded, DescriptionError, MappingError, TransportFailure
from gateway.engine_protocol.types import Credential, JobRef, JobState, JobStatus
from tests.test_engine_protocol_executor import JOB, MODELS, Chunks, conn, desc, fast_job, make, req, run


@pytest.mark.parametrize('kind',['dns','tls','blocked'])
def test_wait_does_not_retry_permanent_transport_failures(kind,monkeypatch):
    ex,_=make(fast_job(0.5,1),lambda r:httpx.Response(200))
    calls=[]
    async def poll(*args):
        calls.append(1)
        raise TransportFailure(kind,'permanent destination refusal')
    monkeypatch.setattr(ex,'poll',poll)
    with pytest.raises(TransportFailure) as raised:
        run(ex.wait(conn(),JobRef('render','1')))
    assert raised.value.kind==kind and calls==[1]


def test_wait_budget_preserves_the_last_scrubbed_failure(monkeypatch):
    ex,_=make(fast_job(0.5,1),lambda r:httpx.Response(200))
    async def poll(*args):
        raise TransportFailure('timeout','engine stalled with fixture-secret')
    monkeypatch.setattr(ex,'poll',poll)
    with pytest.raises(BudgetExceeded) as raised:
        run(ex.wait(conn(credentials={'inference':Credential('fixture-secret')}),JobRef('render','1')))
    assert 'engine stalled with' in str(raised.value)
    assert 'fixture-secret' not in str(raised.value)


def test_poll_interval_floor_prevents_twenty_calls_per_second():
    with pytest.raises(DescriptionError,match='interval_s'):
        desc(fast_job(0.05,86400))
    desc(fast_job(0.5,86400))


@pytest.mark.parametrize('status',[404,409,410])
def test_cancel_is_idempotent_when_the_job_ends_between_poll_and_cancel(status):
    ex,fake=make(JOB,lambda r:httpx.Response(200,json={'state':'busy'}) if r.method=='GET' else httpx.Response(status))
    run(ex.cancel(conn(),JobRef('render','42')))
    assert [(r.method,r.url.path) for r in fake.requests]==[('GET','/jobs/42'),('POST','/jobs/42/cancel')]


@pytest.mark.parametrize('overshoot',[0,0.1])
def test_wait_clamps_sleep_to_remaining_and_never_polls_at_or_after_deadline(monkeypatch,overshoot):
    ex,_=make(fast_job(1,1),lambda r:httpx.Response(200))
    calls=[];sleeps=[]
    async def go():
        loop=asyncio.get_running_loop(); clock=[0.0]
        async def poll(conn,ref):
            calls.append(clock[0]);clock[0]+=0.25
            return JobStatus(ref,JobState.running)
        async def sleep(delay):
            sleeps.append(delay);clock[0]+=delay+overshoot
        with monkeypatch.context() as m:
            m.setattr(loop,'time',lambda:clock[0]);m.setattr(asyncio,'sleep',sleep);m.setattr(ex,'poll',poll)
            with pytest.raises(BudgetExceeded):
                await ex.wait(conn(),JobRef('render','42'))
    run(go())
    assert calls==[0.0]
    assert sleeps==[0.75]


def test_upstream_excerpt_counts_characters_and_never_splits_utf8():
    text='é'*170
    ex,_=make(MODELS,lambda r:httpx.Response(503,text=text))
    from gateway.engine_protocol.errors import UpstreamError
    with pytest.raises(UpstreamError) as raised:
        run(ex.invoke(conn(),req('models')))
    assert str(raised.value).split(': ',1)[1]=='é'*160
    assert '\ufffd' not in str(raised.value)


def test_lone_surrogate_in_path_is_a_normal_mapping_refusal():
    operations={'one':{'class':'generate','request':{'method':'GET','path':'/items/{id}'},'response':{'framing':'json'}}}
    ex,fake=make(operations,lambda r:httpx.Response(200,json={}))
    with pytest.raises(MappingError,match='safe single path segment'):
        run(ex.invoke(conn(),req('one',params={'id':'a\ud800b'})))
    assert not fake.requests


def test_sse_crlf_split_is_preserved_through_executor():
    operations={'one':{'class':'generate','request':{'method':'GET','path':'/stream'},'response':{'framing':'sse','terminal':{'sentinel':'[DONE]'}}}}
    ex,_=make(operations,lambda r:httpx.Response(200,stream=Chunks([b'data: one\r',b'\n\r',b'\ndata: [DONE]\r',b'\n\r',b'\n'])))
    async def go():
        reply=await ex.invoke(conn(),req('one'))
        return [event async for event in reply]
    assert run(go())==[b'one']


@pytest.mark.parametrize('value', ['job%ready', 'job;ready', 'job%2Fready'])
def test_upstream_job_identifiers_are_encoded_once_for_poll_and_cancel(value):
    from urllib.parse import quote
    ex, fake = make(JOB, lambda r: httpx.Response(200, json={'state': 'busy'}))
    run(ex.cancel(conn(), JobRef('render', value)))
    encoded = quote(value, safe='')
    assert [r.url.raw_path.decode() for r in fake.requests] == [
        '/jobs/' + encoded, '/jobs/' + encoded + '/cancel']


@pytest.mark.parametrize('value', ['asset%ready', 'asset;ready', 'asset%2Fready'])
def test_upstream_asset_handles_are_encoded_once(value):
    from urllib.parse import quote
    ex, fake = make(JOB, lambda r: httpx.Response(
        200, content=b'\x89PNG\r\n\x1a\n' + b'\x00' * 32,
        headers={'content-type': 'image/png'}))
    ref = JobRef('render', 'job%;ready')
    status = JobStatus(ref, JobState.succeeded, assets=(value,))
    assets = run(ex.fetch_assets(conn(), ref, status))
    expected = ex.description.operations['render'].job.fetch.path
    expected = expected.replace('{id}', quote(ref.id, safe='')).replace('{asset}', quote(value, safe=''))
    assert assets
    assert [r.url.raw_path.decode() for r in fake.requests] == [expected]


@pytest.mark.parametrize('value', ['user%ready', 'user;ready', 'user%2Fready'])
def test_user_path_values_still_reject_percent_and_semicolon(value):
    operations = {'one': {'class': 'generate', 'request': {'method': 'GET', 'path': '/items/{id}'},
                          'response': {'framing': 'json'}}}
    ex, fake = make(operations, lambda r: httpx.Response(200, json={}))
    with pytest.raises(MappingError):
        run(ex.invoke(conn(), req('one', params={'id': value})))
    assert not fake.requests


@pytest.mark.parametrize('value', ['.', '..', 'a/b', 'a\\b', 'a\nb', 'a\x00b', '\uff0f', '\uff0e'])
def test_upstream_job_path_values_retain_segment_defenses(value):
    ex, fake = make(JOB, lambda r: httpx.Response(200, json={'state': 'busy'}))
    with pytest.raises(MappingError):
        run(ex.poll(conn(), JobRef('render', value)))
    assert not fake.requests


def _credential_runtime(monkeypatch, credential_headers, *, observations=None, selection=None):
    from gateway.engine_protocol import dispatch
    from gateway.engine_protocol.types import Connection
    operations = {
        name: {'class': cls, 'request': {'method': 'GET', 'path': '/' + name},
               'response': {'framing': 'json', 'select': {'value': '/value'}}}
        for name, cls in [('generate', 'generate'), ('select', 'control'), ('health', 'inventory')]
    }
    doc = description.parse_description({
        'id': 'credentials-r2', 'protocol_version': 1, 'executor': 'http-v1',
        'auth': {'inference': {'kind': 'bearer'}, 'control': {'kind': 'bearer'}},
        'operations': operations}, source='credentials-r2')
    service = {'connection': {'credentials': {
        cls: {'secret_ref': 'env:TEST_' + cls.upper(), 'scheme': 'Bearer'}
        for cls in ('inference', 'control')}},
        'observations': observations or {}}
    offering = {'id': 'fixture', 'selection': selection or {}}
    monkeypatch.setattr(dispatch, 'connection_config', lambda service: service['connection'])
    monkeypatch.setattr(dispatch, 'connection_from_record', lambda *args: Connection(address='http://engine.example'))
    monkeypatch.setattr(dispatch, 'observations_config', lambda service: service['observations'])
    monkeypatch.setattr(dispatch, 'offerings_config', lambda service: [offering])
    monkeypatch.setattr(dispatch, 'credential_headers', credential_headers)
    runtime = dispatch.Runtime(dispatch.Target('fixture', service, offering, doc))
    return runtime


def test_runtime_resolves_only_current_operation_credentials(monkeypatch):
    calls = []
    def headers(service, cls):
        calls.append(cls)
        if cls == 'control':
            raise AssertionError('Unused control credential was resolved')
        return {'Authorization': 'Bearer fixture-inference'}
    runtime = _credential_runtime(monkeypatch, headers)
    assert calls == []
    seen = []
    async def invoke(connection, op, request):
        seen.append(connection.credentials)
    monkeypatch.setattr(runtime.adapter, 'invoke', invoke)
    run(runtime.invoke('generate', {}))
    assert calls == ['inference']
    assert list(seen[0]) == ['inference']


def test_runtime_missing_generation_credential_is_typed_and_never_sent(monkeypatch):
    from gateway.engine_protocol.errors import ProtocolError
    runtime = _credential_runtime(monkeypatch, lambda service, cls: {})
    async def invoke(*args):
        pytest.fail('A generation request without its required credential was sent')
    monkeypatch.setattr(runtime.adapter, 'invoke', invoke)
    with pytest.raises(ProtocolError) as raised:
        run(runtime.invoke('generate', {}))
    assert raised.value.code == 'engine_credential_unavailable'


def test_runtime_selection_and_observation_resolve_separate_credentials(monkeypatch):
    from types import SimpleNamespace
    from gateway.engine_protocol import dispatch
    calls = []
    def headers(service, cls):
        calls.append(cls)
        return {'Authorization': 'Bearer fixture-control'} if cls == 'control' else {}
    runtime = _credential_runtime(monkeypatch, headers,
        observations={'ready': {'op': 'health'}}, selection={'mode': 'switch'})
    assert calls == []
    sent = []
    async def invoke(connection, op, request):
        sent.append((op, dict(connection.credentials)))
        return SimpleNamespace(status=200, selected={'value': True})
    monkeypatch.setattr(runtime.adapter, 'invoke', invoke)
    class Checker:
        def __init__(self, *args, **kwargs): pass
        def _check_paths(self, args, roots): return args, []
        def _recheck_paths(self, identities): pass
    monkeypatch.setattr(dispatch, 'EngineControlRuntime', Checker)
    class Switch:
        def __init__(self, *, invoke, observe, **kwargs):
            self.test_invoke, self.test_observe = invoke, observe
        async def select(self, *args, **kwargs):
            await self.test_invoke('select', {})
            await self.test_observe('ready')
    monkeypatch.setattr(dispatch, 'SwitchController', Switch)
    run(runtime.lease(drain=None, lock_for=None))
    assert calls == ['control', 'inference']
    assert list(sent[0][1]) == ['control']
    assert sent[1] == ('health', {})

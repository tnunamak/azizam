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

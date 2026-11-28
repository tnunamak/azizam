"""Plan adjustments are data-selected, disclosed before streaming, and final-attempt-only."""
import asyncio
import json
from dataclasses import replace

import httpx
import pytest

import proxy
from gateway.subscription_kinds import normalize_kind
from tests.support.fake_upstream.route_dispatch import EventStream, loopback_sender
from tests.support.subscription_fakes import KIND, body_of
from tests.test_model_route_dispatch import harness, consume, CREATED, COMPLETE, QUOTA  # noqa: F401
from tests.test_provider_protocol_registry import provider_row
from tests.test_subscription_plan_access import connected, _receipts_table  # noqa: F401
from tests.test_subscription_review_fixes import world, post  # noqa: F401

HEADER = 'x-azizam-dropped-params'
FIELDS = {'max_tokens': 64, 'max_completion_tokens': 64, 'temperature': .2,
          'top_p': .9, 'top_k': 20, 'stop': ['stop'], 'user': 'test-user'}


@pytest.mark.parametrize('surface', ['responses', 'chat'])
@pytest.mark.parametrize('stream', [False, True])
def test_adjustments_are_disclosed_before_client_bytes_and_recorded(harness, surface, stream):
    fields = FIELDS if surface == 'chat' else {key: value for key, value in FIELDS.items() if key not in KIND.unsupported_fields}
    body = {'model': 'logical-test', 'stream': stream, **fields,
            **({'messages': [{'role': 'user', 'content': 'hi'}]} if surface == 'chat'
               else {'input': [{'role': 'user', 'content': 'hi'}]})}
    async def run():
        response = await harness.call(surface=surface, body=body)
        assert response.status_code == 200
        # Assert before advancing the response iterator: headers precede client bytes.
        assert response.headers[HEADER] == ','.join(sorted(fields))
        await consume(response)
    asyncio.run(run())
    sent = body_of(next(r for r in harness.upstream.calls if r.url.path == '/v1/responses'))
    assert not fields.keys() & sent.keys()
    assert sent['model'] == 'gpt-test'
    assert not harness.paid_calls and not harness.reservations
    assert sorted(harness.receipts[0]['ignored_inputs']) == sorted(fields)


@pytest.mark.parametrize('surface,path', [('chat', '/v1/chat/completions'), ('responses', '/v1/responses'), ('messages', '/v1/messages')])
@pytest.mark.parametrize('stream', [False, True])
def test_real_ingresses_accept_and_disclose_client_max_tokens(world, monkeypatch, surface, path, stream):
    service, upstream = world
    monkeypatch.setattr(proxy, '_forward_chat_completions', loopback_sender(proxy.app))
    body = {'model': 'chatgpt-plan/gpt-test', 'max_tokens': 64, 'stream': stream,
            **({'input': [{'role': 'user', 'content': 'hi'}]} if surface == 'responses'
               else {'messages': [{'role': 'user', 'content': 'hi'}]})}
    response = post(path, body, paid=('chatgpt',))
    assert response.status_code == 200, response.text
    expected = ['max_tokens', 'reasoning_effort'] if surface == 'messages' else ['max_tokens']
    assert response.headers[HEADER] == ','.join(expected)
    sent = body_of(next(r for r in upstream.calls if r.url.path == '/v1/responses'))
    assert 'max_tokens' not in sent and 'max_output_tokens' not in sent
    assert upstream.count('/v1/responses') == 1
    assert response.content


@pytest.mark.parametrize('surface', ['responses', 'chat'])
def test_no_header_when_no_field_was_adjusted(harness, surface):
    async def run():
        response = await harness.call(surface=surface)
        assert response.status_code == 200
        assert HEADER not in response.headers
        assert harness.receipts[0]['ignored_inputs'] == {}
    asyncio.run(run())


@pytest.mark.parametrize('stream', [False, True])
def test_quota_fallback_exposes_only_paid_attempt_adjustments(harness, stream):
    harness.upstream.routes[('POST', '/v1/responses')] = lambda req: httpx.Response(
        200, stream=EventStream([QUOTA]), headers={'content-type': 'text/event-stream'})
    async def run():
        response = await harness.call(surface='chat', body={'model': 'logical-test', 'stream': stream,
            'messages': [{'role': 'user', 'content': 'hi'}], 'temperature': .2})
        assert response.status_code == 200
        assert HEADER not in response.headers
        await consume(response)
    asyncio.run(run())
    plan = body_of(next(r for r in harness.upstream.calls if r.url.path == '/v1/responses'))
    assert 'temperature' not in plan
    assert len(harness.paid_calls) == len(harness.reservations) == 1
    assert harness.paid_calls[0]['temperature'] == .2
    assert harness.receipts[0]['ignored_inputs'] == {}
    assert not {'plan_params_not_sent', 'plan_effort_raised_from'} & harness.receipts[0]['metadata'].keys()


@pytest.mark.parametrize('field', ['temperature', 'top_p', 'user', 'max_output_tokens'])
def test_responses_unsupported_fields_still_refuse_before_send(harness, field):
    response = asyncio.run(harness.call(body={'model': 'chatgpt-plan/gpt-test',
        'input': [{'role': 'user', 'content': 'hi'}], field: 'unsupported'}))
    assert response.status_code == 400
    error = json.loads(response.body)['error']
    assert error['code'] == 'plan_unsupported_parameter' and error['param'] == field
    assert error['message'] == f'{field} is not supported for plan models.'
    assert HEADER not in response.headers
    assert harness.upstream.count('/v1/responses') == 0 and not harness.paid_calls


def test_minimal_keeps_the_old_raise_to_lowest_effort_and_discloses_it(harness):
    response = asyncio.run(harness.call(surface='chat', body={'model': 'logical-test',
        'messages': [{'role': 'user', 'content': 'hi'}], 'reasoning_effort': 'minimal'}))
    assert response.status_code == 200
    assert response.headers[HEADER] == 'reasoning_effort'
    sent = body_of(next(r for r in harness.upstream.calls if r.url.path == '/v1/responses'))
    assert sent['reasoning']['effort'] == 'low'
    assert list(harness.receipts[0]['ignored_inputs']) == ['reasoning_effort']
    assert not harness.paid_calls


def test_new_drop_policy_is_only_kind_data(harness):
    harness.service.kinds[KIND.id] = replace(KIND, dropped_fields=KIND.dropped_fields | {'custom_sampler'})
    response = asyncio.run(harness.call(surface='chat', body={'model': 'logical-test',
        'messages': [{'role': 'user', 'content': 'hi'}], 'custom_sampler': 4}))
    assert response.status_code == 200 and response.headers[HEADER] == 'custom_sampler'
    sent = body_of(next(r for r in harness.upstream.calls if r.url.path == '/v1/responses'))
    assert 'custom_sampler' not in sent


@pytest.mark.parametrize('value', ['temperature', [1], ['not,a,field'], ['bad\r\nheader']])
def test_drop_policy_rejects_non_lists_or_unsafe_header_names(value):
    row = provider_row()
    row['dropped_fields'] = value
    with pytest.raises(ValueError, match='dropped_fields'):
        normalize_kind(row)


def test_final_plan_quota_response_keeps_adjustments_when_paid_fallback_is_denied(harness):
    from tests.support import access as acc
    harness.identity = acc.identity(acc.plan_for(connections=['chatgpt-owner'], paid=['cloud'], fallback=False))
    harness.upstream.routes[('POST', '/v1/responses')] = lambda req: httpx.Response(
        200, stream=EventStream([QUOTA]), headers={'content-type': 'text/event-stream'})
    response = asyncio.run(harness.call(surface='chat', body={'model': 'logical-test',
        'messages': [{'role': 'user', 'content': 'hi'}], 'temperature': .2}))
    assert response.status_code == 429
    assert response.headers[HEADER] == 'temperature'
    assert list(harness.receipts[0]['ignored_inputs']) == ['temperature']
    assert harness.receipts[0]['metadata']['plan_params_not_sent'] == ['temperature']
    assert not harness.paid_calls and not harness.reservations


def test_terminal_responses_refusal_names_the_field_even_when_the_model_is_not_a_pin(harness):
    from tests.support import access as acc
    harness.identity = acc.identity(acc.plan_for(connections=['chatgpt-owner']))   # one authorized edge: nothing can advance
    response = asyncio.run(harness.call(body={'model': 'logical-test',
        'input': [{'role': 'user', 'content': 'hi'}], 'temperature': .2}))
    assert response.status_code == 400
    error = json.loads(response.body)['error']
    assert error['param'] == 'temperature' and 'temperature' in error['message']
    assert harness.upstream.count('/v1/responses') == 0 and not harness.paid_calls


def test_pre_send_refusal_claims_no_dropped_field(harness):
    from gateway.provider_connections import SubscriptionError
    async def not_connected(*args, **kwargs):
        raise SubscriptionError(503, 'plan_not_connected', 'The plan is not connected.')
    harness.service.acquire = not_connected
    response = asyncio.run(harness.call(surface='chat', body={'model': 'chatgpt-plan/gpt-test', 'max_tokens': 64,
        'messages': [{'role': 'user', 'content': 'hi'}]}))
    assert response.status_code == 503
    assert HEADER not in response.headers
    assert harness.receipts[0]['ignored_inputs'] == {}
    assert 'plan_params_not_sent' not in harness.receipts[0]['metadata']
    assert harness.upstream.count('/v1/responses') == 0


@pytest.mark.parametrize('failure,status', [(httpx.ConnectTimeout, 504), (httpx.ConnectError, 502)])
def test_transport_failure_after_send_still_discloses_dropped_fields(harness, failure, status):
    def broken(request):
        raise failure('broken', request=request)
    harness.upstream.routes[('POST', '/v1/responses')] = broken
    response = asyncio.run(harness.call(surface='chat', body={'model': 'chatgpt-plan/gpt-test', 'max_tokens': 64,
        'messages': [{'role': 'user', 'content': 'hi'}]}))
    assert response.status_code == status
    assert response.headers[HEADER] == 'max_tokens'
    assert list(harness.receipts[0]['ignored_inputs']) == ['max_tokens']
    assert harness.receipts[0]['metadata']['plan_params_not_sent'] == ['max_tokens']

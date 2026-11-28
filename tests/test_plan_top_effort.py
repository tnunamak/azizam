"""A subscription ceiling lowers the wire effort and discloses the serving attempt."""
import asyncio
import json

import httpx
import pytest

from gateway.provider_protocols import REQUEST_PROTOCOLS
from tests.support.subscription_fakes import KIND, Upstream, connect, make_service, body_of
from tests.test_model_route_dispatch import harness, consume, QUOTA, EventStream  # noqa: F401
from tests.test_subscription_plan_access import connected  # noqa: F401
from tests.test_subscription_review_fixes import world  # noqa: F401
from tests.test_subscription_plan_usage import _client


@pytest.mark.parametrize('protocol_id', ['openai-responses-plan', 'claude-code-runner'])
@pytest.mark.parametrize('surface', ['chat', 'responses'])
@pytest.mark.parametrize('requested,ceiling,advertised,expected,lowered', [
    ('xhigh', 'medium', ['high', 'low', 'medium', 'xhigh'], 'medium', True),
    ('medium', 'medium', ['low', 'medium', 'high'], 'medium', False),
    ('low', 'medium', ['low', 'medium', 'high'], 'low', False),
    ('high', None, ['low', 'high'], 'high', False),
    ('xhigh', 'none', ['xhigh', 'high'], 'high', True),
    ('high', 'low', ['low', 'medium'], 'low', True),
    ('medium', 'low', ['xhigh', 'high'], 'high', True),
    ('high', 'low', ['xhigh', 'high'], 'high', False),
])
def test_protocol_ceiling(protocol_id, surface, requested, ceiling, advertised, expected, lowered):
    protocol = REQUEST_PROTOCOLS[protocol_id]
    payload = {'reasoning': {'effort': requested, 'summary': 'auto'}} if protocol_id == 'openai-responses-plan' else {'effort': requested}
    facts = protocol.apply_model_facts(surface, payload, advertised, {}, top_effort=ceiling)
    actual = payload['reasoning']['effort'] if protocol_id == 'openai-responses-plan' else payload['effort']
    assert actual == expected
    assert facts == ({'plan_effort_lowered_from': requested} if lowered else {})
    if protocol_id == 'openai-responses-plan':
        assert payload['reasoning']['summary'] == 'auto'



@pytest.mark.parametrize('protocol_id', ['openai-responses-plan', 'claude-code-runner'])
@pytest.mark.parametrize('surface', ['chat', 'responses'])
def test_a_request_at_or_below_the_ceiling_is_treated_as_with_no_ceiling(protocol_id, surface):
    """The ceiling only lowers. A level the model does not offer is refused, as without a ceiling; it is not "lowered"
    up to the model's least level."""
    from gateway.provider_protocols import RequestError

    def payload():
        return {'reasoning': {'effort': 'low'}} if protocol_id == 'openai-responses-plan' else {'effort': 'low'}
    protocol = REQUEST_PROTOCOLS[protocol_id]
    outcomes = []
    for ceiling in (None, 'low', 'medium'):
        try:
            outcomes.append(('ok', dict(protocol.apply_model_facts(surface, payload(), ['high', 'xhigh'], {}, top_effort=ceiling))))
        except RequestError as error:
            outcomes.append(('refused', error.code))
    assert outcomes[1] == outcomes[0] and outcomes[2] == outcomes[0]
    assert all('plan_effort_lowered_from' not in str(outcome) for outcome in outcomes)

@pytest.mark.parametrize('ceiling', ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', None])
def test_store_round_trip_refresh_and_reconnect(tmp_path, ceiling):
    upstream = Upstream()
    # Use the same injected transport for the connect and refresh calls.
    service, store, jwks = make_service(tmp_path, upstream)
    asyncio.run(connect(service, jwks, upstream))
    store.set_top_effort('chatgpt-owner', ceiling)
    assert store.connection('chatgpt')['top_effort'] == ceiling
    asyncio.run(service.refresh_models('chatgpt'))
    assert asyncio.run(service.status_one('chatgpt'))['top_effort'] == ceiling
    asyncio.run(connect(service, jwks, upstream))
    assert store.connection('chatgpt-owner')['top_effort'] == ceiling
    with pytest.raises(ValueError, match='effort'):
        store.set_top_effort('chatgpt', 'ultra')


@pytest.mark.parametrize('surface', ['chat', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
def test_lowering_reaches_header_and_receipt_without_dropped_effort(harness, surface, stream):
    harness.service.store.set_top_effort('chatgpt', 'low')
    body = {'model': 'logical-test', 'stream': stream,
            **({'messages': [{'role': 'user', 'content': 'hi'}], 'reasoning_effort': 'high'} if surface == 'chat'
               else {'input': [{'role': 'user', 'content': 'hi'}], 'reasoning': {'effort': 'high'}})}
    async def run():
        response = await harness.call(surface=surface, body=body)
        assert response.status_code == 200
        assert response.headers['x-azizam-plan-effort-lowered-from'] == 'high'
        assert 'x-azizam-dropped-params' not in response.headers
        await consume(response)
    asyncio.run(run())
    sent = body_of(next(r for r in harness.upstream.calls if r.url.path == '/v1/responses'))
    assert sent['reasoning']['effort'] == 'low'
    receipt = harness.receipts[0]
    assert receipt['metadata']['plan_effort_lowered_from'] == 'high'
    assert 'reasoning_effort' not in receipt['ignored_inputs']
    assert 'plan_params_not_sent' not in receipt['metadata']


def test_paid_fallback_refusal_does_not_disclose_plan_lowering(harness):
    harness.service.store.set_top_effort('chatgpt', 'low')
    harness.upstream.routes[('POST', '/v1/responses')] = lambda req: httpx.Response(
        200, stream=EventStream([QUOTA]), headers={'content-type': 'text/event-stream'})
    response = asyncio.run(harness.call(surface='chat', body={'model': 'logical-test',
        'messages': [{'role': 'user', 'content': 'hi'}], 'reasoning_effort': 'high'}))
    assert response.status_code == 400
    assert json.loads(response.body)['error']['code'] == 'paid_option_unsupported'
    assert 'x-azizam-plan-effort-lowered-from' not in response.headers
    assert 'plan_effort_lowered_from' not in harness.receipts[0]['metadata']
    assert not harness.paid_calls


@pytest.mark.parametrize('value', ['ultra', '', 1, [], {}])
def test_api_refuses_unknown_effort(tmp_path, value):
    service, _, _ = make_service(tmp_path, Upstream())
    response = _client(service, {'owner'}).patch('/admin/api/subscriptions/chatgpt/reasoning',
        headers={'x-test-who': '1'}, json={'top_effort': value})
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'invalid_top_effort'


def test_owner_api_saves_clears_and_checks_role(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    asyncio.run(connect(service, jwks, upstream))
    path = '/admin/api/subscriptions/chatgpt/reasoning'
    headers = {'x-test-who': '1'}
    denied = _client(service, {'admin'}).patch(path, headers=headers, json={'top_effort': 'low'})
    assert denied.status_code == 403
    client = _client(service, {'owner'})
    for value in ['low', None]:
        response = client.patch(path, headers=headers, json={'top_effort': value})
        assert response.status_code == 200
        assert response.json()['subscription']['top_effort'] == value
        assert store.connection('chatgpt')['top_effort'] == value
    assert client.patch(path, headers=headers, json={}).status_code == 400


@pytest.mark.parametrize('audience', ['admin', 'client'])
def test_visible_receipt_discloses_only_known_efforts(audience):
    from gateway.receipts import serialize_receipt_for_audience
    for value, expected in [('high', 'high'), ('bad\r\nvalue', None), ([], None)]:
        shown = serialize_receipt_for_audience({'metadata': {'plan_effort_lowered_from': value}}, audience)
        assert shown.get('plan_effort_lowered_from') == expected


@pytest.mark.parametrize('stream', [False, True])
def test_messages_facade_keeps_the_lowering_header(world, monkeypatch, stream):
    import proxy
    from tests.support.fake_upstream.route_dispatch import loopback_sender
    from tests.test_subscription_review_fixes import post
    service, upstream = world
    service.store.set_top_effort('chatgpt', 'low')
    monkeypatch.setattr(proxy, '_forward_chat_completions', loopback_sender(proxy.app))
    response = post('/v1/messages', {'model': 'chatgpt-plan/gpt-test', 'stream': stream,
        'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 64,
        'thinking': {'type': 'enabled', 'budget_tokens': 16384}}, paid=('chatgpt',))
    assert response.status_code == 200, response.text
    assert response.headers['x-azizam-plan-effort-lowered-from'] == 'high'
    assert response.headers['x-azizam-dropped-params'] == 'max_tokens'

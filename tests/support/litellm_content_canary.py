"""Subprocess canary: real pinned proxy, fake protocol engine and Prisma sinks.

Run in a fresh private cwd with an allowlisted env. No sockets or external DB.
The parent scans captured stdout/stderr and every resulting file.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import yaml

from gateway.litellm_substrate import compile_litellm_substrate_config
from tests.support.fake_upstream import FakeUpstream, VirtualClock, scenario_from_data

MARKER = 'RETENTION_CANARY_8bf972_nested_prompt'


async def run() -> None:
    assert importlib.metadata.version('litellm') == '1.86.2'
    import litellm
    from litellm.proxy import proxy_server as ps
    from litellm.proxy._types import UserAPIKeyAuth
    from litellm.proxy.hooks.proxy_track_cost_callback import _ProxyDBLogger
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
    from litellm.proxy.auth.auth_checks import _virtual_key_max_budget_check

    positive = os.environ.get('CANARY_POSITIVE') == 'true'
    compiled = compile_litellm_substrate_config({})
    compiled['model_list'] = [{'model_name': 'canary', 'litellm_params': {
        'model': 'openai/canary', 'api_base': 'http://engine.fk.test/v1', 'api_key': 'fake-only',
        'max_retries': 0, 'input_cost_per_token': 0.001, 'output_cost_per_token': 0.001}}]
    compiled['router_settings']['num_retries'] = 0
    if positive:
        compiled['general_settings']['disable_spend_logs'] = False
        compiled['general_settings']['disable_error_logs'] = False
        compiled['general_settings']['store_prompts_in_spend_logs'] = True
        compiled['litellm_settings']['turn_off_message_logging'] = False
    config = Path('generated.yaml')
    config.write_text(yaml.safe_dump(compiled))
    await ps.initialize(config=str(config))
    assert ps.disable_spend_logs is (not positive)
    assert ps.general_settings['disable_error_logs'] is (not positive)
    assert litellm.turn_off_message_logging is (not positive)
    assert litellm.cache is None

    scenario = scenario_from_data({'protocol': 'openai', 'name': 'retention-canary',
        'provenance': {'source': 'hand-written', 'reference': 'REQUEST_RETENTION 5.2 / 16.2'},
        'exchanges': [
            {'id': 'success', 'match': {'method': 'POST', 'path': '/v1/chat/completions'}, 'response': {'status': 200,
                'json': {'id': 'canary-response', 'object': 'chat.completion', 'created': 1, 'model': 'canary',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': MARKER}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}}},
            {'id': 'error', 'repeat': True, 'match': {'method': 'POST', 'path': '/v1/chat/completions'},
             'response': {'status': 400, 'json': {'error': {'message': 'echo: ' + MARKER, 'type': 'invalid_request_error'}}}},
        ]})
    fake = FakeUpstream(scenario, VirtualClock(), 'engine')
    engine_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake))
    # Only the network edge is replaced. The real proxy, router, exception
    # handling, success logger, failure hook and spend writer all execute.
    original_create = AsyncHTTPHandler.create_client
    AsyncHTTPHandler.create_client = lambda *args, **kwargs: engine_client
    tables: dict[str, list] = {}
    updates = []
    class Table:
        def __init__(self, name): self.name = name
        async def create(self, **kwargs): tables.setdefault(self.name, []).append(kwargs)
        async def create_many(self, **kwargs): tables.setdefault(self.name, []).append(kwargs)
        async def find_unique(self, **kwargs): return None
        async def find_first(self, **kwargs): return None
    class Database:
        def __getattr__(self, name): return Table(name)
    prisma = SimpleNamespace(db=Database(), spend_log_transactions=[], _spend_log_transactions_lock=asyncio.Lock())
    ps.prisma_client = prisma
    writer = ps.proxy_logging_obj.db_spend_update_writer
    original_queue = writer.spend_update_queue.add_update
    async def record_update(*, update):
        updates.append(update.model_dump())
        await original_queue(update=update)
    writer.spend_update_queue.add_update = record_update

    token = UserAPIKeyAuth(api_key='fake-virtual-key', token='fake-virtual-key', user_id='canary-user', team_id='canary-team',
                           max_budget=0.01, spend=0.0, request_route='/v1/chat/completions')
    async def authenticate(): return token
    ps.app.dependency_overrides[ps.user_api_key_auth] = authenticate
    logger = _ProxyDBLogger()
    litellm.callbacks.append(logger)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ps.app), base_url='http://proxy.test') as client:
        payload = {'model': 'canary', 'messages': [{'role': 'user', 'content': MARKER}]}
        success = await client.post('/v1/chat/completions', json=payload)
        assert success.status_code == 200, success.status_code
        assert success.json()['choices'][0]['message']['content'] == MARKER
        error = await client.post('/v1/chat/completions', json=payload)
        assert error.status_code == 400, error.status_code
    # Exercise the actual failure hook with a traceback-bearing error. The
    # normal proxy error route invokes it too; this pins its DB opt-out seam.
    await logger.async_post_call_failure_hook(request_data={'model': 'canary', 'messages': payload['messages']},
        original_exception=ValueError(MARKER), user_api_key_dict=token, traceback_str=MARKER)
    # Wait for LiteLLM's asynchronous success logging without sleeping a fixed interval.
    for _ in range(500):
        if {'key', 'user', 'team'} <= {str(item['entity_type']).lower() for item in updates}:
            break
        await asyncio.sleep(0.01)
    assert {'key', 'user', 'team'} <= {str(item['entity_type']).lower() for item in updates}, updates
    assert all(item['response_cost'] > 0 for item in updates), updates
    # The pinned budget guard reads the counters still incremented by the
    # success logger even though per-request spend rows have been disabled.
    async def get_spend(*, counter_key, fallback_spend):
        from litellm.proxy.proxy_server import get_current_spend
        return fallback_spend
    # Use actual queued cost as DB fallback, avoiding a fabricated spend value.
    token.spend = sum(item['response_cost'] for item in updates if str(item['entity_type']).lower() == 'key')
    try:
        await _virtual_key_max_budget_check(token, ps.proxy_logging_obj, None)
    except litellm.BudgetExceededError:
        budget_refused = True
    else:
        budget_refused = False
    assert budget_refused
    tables['LiteLLM_SpendLogs'] = prisma.spend_log_transactions
    # Persist only what reached a database write/queue, never the responses.
    Path('database.json').write_text(json.dumps(tables, default=str))
    Path('budget.json').write_text(json.dumps({'updates': updates, 'budget_refused': budget_refused}, default=str))
    assert len(fake.requests) >= 2
    assert all(MARKER.encode() in item.body for item in fake.requests)
    assert not fake.violations
    await engine_client.aclose()
    AsyncHTTPHandler.create_client = original_create
    print(json.dumps({'canary': 'complete', 'requests': len(fake.requests), 'spend_rows': len(prisma.spend_log_transactions),
                      'error_rows': sum(len(rows) for name, rows in tables.items() if 'error' in name.lower()),
                      'budget_refused': budget_refused}))


if __name__ == '__main__':
    asyncio.run(run())

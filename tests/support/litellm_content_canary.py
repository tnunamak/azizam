"""Subprocess canary: real pinned proxy, fake protocol engine and Prisma sinks.

Run in a fresh private cwd with an allowlisted env. No sockets or external DB.
The parent scans captured stdout/stderr and every resulting file.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import yaml

from gateway.litellm_substrate import compile_litellm_substrate_config
from tests.support.fake_upstream import FakeUpstream, VirtualClock, scenario_from_data

MARKER = 'RETENTION_CANARY_8bf972_nested_prompt'
TOOL_NAME = 'retention_canary_tool'
CONTENT_MARKERS = {
    'prompt': MARKER,
    'tool_name': TOOL_NAME,
    'arguments': 'RETENTION_CANARY_8bf972_tool_arguments',
    'description': 'RETENTION_CANARY_8bf972_tool_description',
    'schema': 'RETENTION_CANARY_8bf972_tool_schema',
    'result': 'RETENTION_CANARY_8bf972_tool_result',
}


async def run() -> None:
    assert importlib.metadata.version('litellm') == '1.86.2'
    import litellm
    from litellm.proxy import proxy_server as ps
    from litellm.proxy._types import UserAPIKeyAuth
    from litellm.proxy.hooks.proxy_track_cost_callback import _ProxyDBLogger
    from litellm.proxy.auth.auth_checks import _virtual_key_max_budget_check

    litellm.disable_aiohttp_transport = True  # in-memory httpx transport replaces network only
    positive = os.environ.get('CANARY_POSITIVE') == 'true'
    if not positive:
        from gateway.litellm_launcher import install_no_content_shim
        install_no_content_shim()
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

    scenario = scenario_from_data({'protocol': 'openai-compatible', 'name': 'retention-canary',
        'provenance': {'source': 'hand-written', 'reference': 'REQUEST_RETENTION 5.2 / 16.2'},
        'exchanges': [
            {'id': 'success', 'match': {'method': 'POST', 'path': '/v1/chat/completions'}, 'response': {'status': 200,
                'json': {'id': 'canary-response', 'object': 'chat.completion', 'created': 1, 'model': 'canary',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': MARKER, 'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': TOOL_NAME, 'arguments': json.dumps({'argument': CONTENT_MARKERS['arguments']})}}]}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}}},
            {'id': 'error', 'repeat': True, 'match': {'method': 'POST', 'path': '/v1/chat/completions'},
             'response': {'status': 400, 'json': {'error': {'message': 'echo: ' + MARKER, 'type': 'invalid_request_error'}}}},
        ]})
    fake = FakeUpstream(scenario, VirtualClock(), 'engine')
    engine_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake))
    # Only the network edge is replaced. The real proxy, router, exception
    # handling, success logger, failure hook and spend writer all execute.
    original_send = httpx.AsyncClient.send
    async def route(self, request, **kwargs):
        if request.url.host == 'engine.fk.test':
            return await original_send(engine_client, request, **kwargs)
        if request.url.host == 'proxy.test':
            return await original_send(self, request, **kwargs)
        raise AssertionError('unexpected outbound HTTP host')
    httpx.AsyncClient.send = route
    schema = (Path(litellm.__file__).parent / 'proxy/schema.prisma').read_text()
    table_names = {name.lower(): name for name in re.findall(r'^model (\w+)', schema, re.M)}
    tables: dict[str, list] = {name: [] for name in table_names.values()}
    updates = []
    class Table:
        def __init__(self, name): self.name = table_names[name]
        async def create(self, **kwargs): tables.setdefault(self.name, []).append(kwargs)
        async def create_many(self, **kwargs):
            tables[self.name].append(kwargs)
            return len(kwargs.get('data', []))
        async def upsert(self, **kwargs):
            tables[self.name].append(kwargs)
            return SimpleNamespace(**kwargs.get('data', {}).get('create', {}))
        async def find_unique(self, **kwargs): return None
        async def find_first(self, **kwargs): return None
    class Database:
        def __getattr__(self, name): return Table(name)
    prisma = SimpleNamespace(db=Database(), spend_log_transactions=[], _spend_log_transactions_lock=asyncio.Lock())
    from litellm.proxy.utils import PrismaClient
    prisma.get_request_status = lambda payload: PrismaClient.get_request_status(prisma, payload)
    prisma.jsonify_object = lambda data: PrismaClient.jsonify_object(prisma, data)
    ps.prisma_client = prisma
    writer = ps.proxy_logging_obj.db_spend_update_writer
    for name, queue in vars(writer).items():
        if name.startswith('daily_') and hasattr(queue, 'add_update'):
            original_add = queue.add_update
            async def record_daily(update, *, table=name, original=original_add):
                tables.setdefault(table, []).append(update.model_dump() if hasattr(update, 'model_dump') else dict(update))
                await original(update=update)
            queue.add_update = record_daily
    batch_done = asyncio.Event()
    original_batch = writer._batch_database_updates
    async def batch(**kwargs):
        await original_batch(**kwargs)
        batch_done.set()
    writer._batch_database_updates = batch
    original_queue = writer.spend_update_queue.add_update
    async def record_update(*, update):
        recorded = dict(update)
        recorded["entity_type"] = getattr(recorded["entity_type"], "value", recorded["entity_type"])
        updates.append(recorded)
        await original_queue(update=update)
    writer.spend_update_queue.add_update = record_update

    token = UserAPIKeyAuth(api_key='fake-virtual-key', token='fake-virtual-key', user_id='canary-user', team_id='canary-team',
                           max_budget=0.01, spend=0.0, request_route='/v1/chat/completions')
    async def authenticate(): return token
    ps.app.dependency_overrides[ps.user_api_key_auth] = authenticate
    logger = _ProxyDBLogger()
    litellm.callbacks.append(logger)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ps.app), base_url='http://proxy.test') as client:
        payload = {'model': 'canary', 'tools': [{'type': 'function', 'function': {
            'name': TOOL_NAME, 'description': CONTENT_MARKERS['description'],
            'parameters': {'type': 'object', 'properties': {'argument': {'type': 'string',
                'description': CONTENT_MARKERS['schema']}}, 'required': ['argument']}}}],
            'messages': [{'role': 'user', 'content': MARKER},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'prior-call', 'type': 'function',
                    'function': {'name': TOOL_NAME, 'arguments': json.dumps({'argument': CONTENT_MARKERS['arguments']})}}]},
                {'role': 'tool', 'tool_call_id': 'prior-call', 'content': CONTENT_MARKERS['result']}]}

        success = await client.post('/v1/chat/completions', json=payload)
        assert success.status_code == 200, success.text
        assert success.json()['choices'][0]['message']['content'] == MARKER
        error = await client.post('/v1/chat/completions', json=payload)
        assert error.status_code == 400, error.status_code
    # Exercise the actual failure hook with a traceback-bearing error. The
    # normal proxy error route invokes it too; this pins its DB opt-out seam.
    await logger.async_post_call_failure_hook(request_data={'model': 'canary', 'messages': payload['messages']},
        original_exception=ValueError(MARKER), user_api_key_dict=token, traceback_str=MARKER)
    await asyncio.wait_for(batch_done.wait(), timeout=5)
    assert {'key', 'user', 'team'} <= {getattr(item['entity_type'], 'value', item['entity_type']) for item in updates}, updates
    assert all(item['response_cost'] >= 0 for item in updates), updates
    assert sum(item['response_cost'] for item in updates if getattr(item['entity_type'], 'value', item['entity_type']) == 'key') == 0.015
    # The pinned budget guard reads the counters still incremented by the
    # success logger even though per-request spend rows have been disabled.
    # Use actual queued cost as DB fallback, avoiding a fabricated spend value.
    token.spend = sum(item['response_cost'] for item in updates if getattr(item['entity_type'], 'value', item['entity_type']) == 'key')
    try:
        await _virtual_key_max_budget_check(token, ps.proxy_logging_obj, None)
    except litellm.BudgetExceededError:
        budget_refused = True
    else:
        budget_refused = False
    assert budget_refused
    # Drain the real tool writer and spend/index job. Every table method is a
    # recording fake; this observes persistence arguments before serialization.
    await writer._flush_tool_discovery_queue(prisma_client=prisma)
    from litellm.proxy.utils import update_spend_logs_job
    await update_spend_logs_job(prisma_client=prisma, db_writer_client=None, proxy_logging_obj=ps.proxy_logging_obj)
    assert prisma.spend_log_transactions == []
    assert bool(tables['LiteLLM_ToolTable']) is positive
    if not positive:
        assert not tables['LiteLLM_SpendLogToolIndex']
    assert not tables['LiteLLM_SpendLogGuardrailIndex']
    # Persist only what reached a database write/queue, never the responses.
    Path('database.json').write_text(json.dumps(tables, default=str))
    Path('budget.json').write_text(json.dumps({'updates': updates, 'budget_refused': budget_refused}, default=str))
    assert len(fake.requests) >= 2
    assert all(all(value.encode() in item.body for value in CONTENT_MARKERS.values()) for item in fake.requests)
    assert not fake.violations
    await engine_client.aclose()
    httpx.AsyncClient.send = original_send
    print(json.dumps({'canary': 'complete', 'requests': len(fake.requests), 'spend_rows': len(tables['LiteLLM_SpendLogs']),
                      'error_rows': sum(len(rows) for name, rows in tables.items() if 'error' in name.lower()),
                      'budget_refused': budget_refused}))


if __name__ == '__main__':
    asyncio.run(run())

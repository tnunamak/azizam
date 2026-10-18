"""Bounded, partial model discovery with a short cache per engine configuration."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Mapping

from gateway.engine_contract_view import connection_config, offerings_config
from gateway.engine_protocol import description, dispatch
from gateway.engine_protocol.errors import ProtocolError
from gateway.engine_protocol.types import StreamReply

ENGINE_TIMEOUT_S = 1.0
LIST_TIMEOUT_S = 1.5
CACHE_TTL_S = 15.0
MAX_CACHED_ENGINES = 256
_cache: OrderedDict[str, tuple[float, list[dict], description.ProtocolDescription]] = OrderedDict()
_issues: dict[str, str] = {}
_LOG = logging.getLogger(__name__)


def problems() -> list[dict[str, str]]:
    return [{'engine_id': engine_id, 'reason': reason} for engine_id, reason in _issues.items()]


def configuration_problems(registry: Mapping[str, Any]) -> list[dict[str, str]]:
    """Report refused stored records without probing or preventing other records from loading."""
    from gateway.service_registry import validate_protocol_dispatch_local_only
    issues = []
    for engine_id, service in (registry.get('services') or {}).items():
        if not dispatch.opted_in(service) or not service.get('enabled', True):
            continue
        try:
            validate_protocol_dispatch_local_only(service)
        except ValueError:
            issues.append({'engine_id': engine_id,
                'reason': 'Protocol dispatch is refused for paid, remote or unproven addresses. Use the provider route or a loopback engine.'})
    return issues


def collection(payload: Any) -> list[Mapping]:
    items = payload.get('data') if isinstance(payload, Mapping) else None
    if (not isinstance(items, list) or any(not isinstance(item, Mapping) or
            not isinstance(item.get('id'), str) or not item['id'] for item in items)):
        raise ProtocolError('The model response needs a data array with a string id per object.', code='malformed_response')
    return items


async def models(registry: Mapping) -> list[dict]:
    now = time.monotonic()
    for key, cached in list(_cache.items()):
        if now - cached[0] >= CACHE_TTL_S:
            del _cache[key]
    docs = await asyncio.to_thread(description.load_registry)
    services = {key: row for key, row in (registry.get('services') or {}).items()
                if dispatch.opted_in(row) and row.get('enabled', True)}
    for stale in _issues.keys() - services.keys():
        _issues.pop(stale, None)

    async def one(engine_id, service):
        doc = docs.get(connection_config(service).get('protocol'))
        if doc is None:
            _issues[engine_id] = 'The configured protocol description is unavailable.'
            return []
        op = doc.operations.get('models')
        if doc.executor != 'http-v1' or op is None:
            _issues.pop(engine_id, None)
            return []
        if op.op_class != 'inventory':
            _issues[engine_id] = 'Model listing needs an inventory operation.'
            return []
        offerings = offerings_config(service)
        if not offerings:
            _issues.pop(engine_id, None)
            return []
        # Include credential refs, address, offerings and the compiled document.
        # Configuration changes cannot reuse a previous origin's inventory.
        key = json.dumps([engine_id, service, id(doc)], sort_keys=True)
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_S:
            _cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        try:
            async with asyncio.timeout(ENGINE_TIMEOUT_S):
                from gateway.service_registry import validate_protocol_dispatch_local_only
                validate_protocol_dispatch_local_only(service)
                runtime = await asyncio.to_thread(dispatch.Runtime, dispatch.Target(engine_id, service, offerings[0], doc))
                reply = await runtime.invoke('models', {})
                if isinstance(reply, StreamReply):
                    await reply.aclose()
                    raise ProtocolError('Model listing cannot be streamed.', code='malformed_response')
                offered = {str(o.get('upstream') or o['id']): o for o in offerings}
                rows = []
                for item in collection(reply.body):
                    offering = offered.get(item['id']) or next((o for o in offerings if o['id'] == item['id']), None)
                    if offering:
                        rows.append({**item, 'id': offering['id']})
            _issues.pop(engine_id, None)
            _cache[key] = (time.monotonic(), rows, doc)
            while len(_cache) > MAX_CACHED_ENGINES:
                _cache.popitem(last=False)
            return copy.deepcopy(rows)
        except Exception as error:  # one engine cannot take the aggregate down; cancellation still propagates
            _LOG.warning('Protocol inventory failed for %s: %s', engine_id, error)
            _issues[engine_id] = 'The engine model list is unavailable or invalid.'
            return []

    tasks = {asyncio.create_task(one(key, row)): key for key, row in services.items()}
    if not tasks:
        return []
    try:
        done, pending = await asyncio.wait(tasks, timeout=LIST_TIMEOUT_S)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for task in pending:
        _issues[tasks[task]] = 'The model listing exceeded its total time limit.'
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    # Registry order is stable even though network calls complete out of order.
    return [item for task in tasks if task in done for item in task.result()]

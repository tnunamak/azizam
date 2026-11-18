"""Last-known choices and nonblocking, per-service refresh."""
import asyncio
import logging
import time

from gateway.engine_protocol.choice_refresh import is_due, refresh_service
from gateway.engine_protocol.choice_store import ChoiceStore, snapshot_choices
from gateway.engine_protocol.node_info import fetch_fresh

# The process store of last-known engine choices. Unset (None) means no snapshots and no refresh; the gateway sets it at
# startup, as it does the node info disk copy, so a test that never sets it neither reads nor writes a shared file.
STORE: ChoiceStore | None = None
_tasks = {}
_log = logging.getLogger(__name__)


def choices_for_run(identity, inputs):
    store = STORE
    if store is None or identity is None:
        return {}
    service = identity.service_id
    choices = snapshot_choices(store, service, inputs)
    if not identity.origin:
        return choices
    now = time.time()
    declarations = []
    for spec in inputs.values():
        declaration = spec.get('choices_from') if isinstance(spec, dict) else None
        if declaration and declaration not in declarations and is_due(store, service, declaration['node_type'], declaration['input'], now=now):
            declarations.append(declaration)
    if declarations and service not in _tasks:
        async def refresh():
            try:
                report = await asyncio.to_thread(refresh_service, store, service, declarations,
                    fetch=lambda _service, node: fetch_fresh(identity.origin, node), now=now)
                for reason in report.values():
                    if reason:
                        _log.warning('Choice refresh failed for service %s: %s', service, reason)
            except Exception:
                _log.exception('Choice refresh failed for service %s', service)
            finally:
                _tasks.pop(service, None)
        _tasks[service] = asyncio.create_task(refresh())
    return choices


async def owner_choices(entries, resolve_identity, *, refresh=False):
    """Describe declared inputs, optionally joining or starting service refreshes."""
    from collections.abc import Mapping
    from gateway.engine_protocol.choice_refresh import freshness

    store = STORE
    rows, groups = [], {}
    for entry in entries:
        try:
            identity = await asyncio.to_thread(resolve_identity, entry)
        except Exception:
            identity = None
        inputs = entry.contract.get('inputs', {})
        for name, spec in inputs.items():
            declaration = spec.get('choices_from') if isinstance(spec, Mapping) else None
            if not declaration:
                continue
            declaration = dict(declaration)
            service = identity.service_id if identity else None
            rows.append((entry.id, name, declaration, identity))
            if service:
                group = groups.setdefault(service, (identity, []))[1]
                if declaration not in group:
                    group.append(declaration)
    if refresh and store is not None:
        async def start(service, identity, declarations):
            task = _tasks.get(service)
            if task is None:
                def fetch(_service, node):
                    if not identity.origin:
                        raise ValueError('no service address')
                    return fetch_fresh(identity.origin, node)
                async def run():
                    try:
                        await asyncio.to_thread(refresh_service, store, service, declarations, fetch=fetch, now=time.time())
                    finally:
                        _tasks.pop(service, None)
                task = _tasks[service] = asyncio.create_task(run())
            await asyncio.shield(task)
        await asyncio.gather(*(start(service, identity, declarations) for service, (identity, declarations) in groups.items()))
    result = []
    now = time.time()
    for workflow_id, name, declaration, identity in rows:
        service = identity.service_id if identity else None
        snapshot = store.read(service, declaration['node_type'], declaration['input']) if store is not None and service else None
        if store is not None and service:
            data = freshness(store, service, [declaration], now=now)[0]
            state = 'failed' if data['failure_reason'] else 'missing' if snapshot is None else 'stale' if data['stale'] else 'fresh'
        else:
            data = {'fetched_at': None, 'age_s': None, 'failure_reason': 'no bound service' if not service else None, 'failed_at': None}
            state = 'failed' if not service else 'missing'
        result.append({'workflow_id': workflow_id, 'input': name, 'node_type': declaration['node_type'],
                       'service_id': service, 'choices': snapshot.choices if snapshot else None,
                       'fetched_at': data['fetched_at'], 'age_s': data['age_s'], 'state': state,
                       'failure_reason': data['failure_reason'], 'failed_at': data['failed_at']})
    return result

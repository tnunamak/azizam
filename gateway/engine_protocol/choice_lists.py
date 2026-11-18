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

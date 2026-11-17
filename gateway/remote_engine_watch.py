"""One revocation poller for each remote service/database on a serving loop."""
from __future__ import annotations

import asyncio
import weakref

from gateway.engine_protocol.errors import ProtocolError

_WATCHERS = weakref.WeakKeyDictionary()


def service_key(bound):
    from pathlib import Path
    path = bound.factory.db_path
    return (str(Path(path).resolve()) if path is not None else id(bound.factory), bound.capability.service_id)


class ServiceWatcher:
    def __init__(self):
        self.bindings = weakref.WeakSet()
        self.task = None

    def _check(self, active):
        # One worker submission per service tick, never one per transport.
        for bound in active:
            if bound.revoked:
                continue
            try:
                bound.factory.check(bound.capability, service_id=bound.capability.service_id,
                                    address=bound.capability.exact_origin)
                bound.budget.remaining()
            except Exception:
                bound.revoke()  # unreadable authority is not permission to continue

    async def run(self):
        while True:
            active = tuple(b for b in self.bindings if not b.revoked and (b.tasks or b.responses or b.clients))
            if not active:
                return
            delay = min(b.budget.limits['authority_check_s'] for b in active)
            await asyncio.sleep(delay)
            await asyncio.to_thread(self._check, active)


def watch(bound):
    loop = asyncio.get_running_loop()
    watchers = _WATCHERS.setdefault(loop, weakref.WeakValueDictionary())
    key = service_key(bound)
    watcher = watchers.get(key)
    if watcher is None:
        watcher = ServiceWatcher()
        watchers[key] = watcher
    watcher.bindings.add(bound)
    if watcher.task is None or watcher.task.done():
        watcher.task = loop.create_task(watcher.run())
    # Keep the watcher alive as long as its bindings own work.
    bound.watcher = watcher
    return watcher.task

"""Service-bound authority for declared remote operations.

Admission is separate from production routing. A factory reads owner-controlled
registry state, never caller URLs or headers. Candidate use is confined to the
isolated test harness until the release gate permits production admission.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import asyncio
import time
import threading
import weakref
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

import yaml

from gateway.engine_contract_view import connection_config, offerings_config
from gateway.engine_protocol.errors import ProtocolError

_FACTORIES = weakref.WeakSet()
_FACTORY_LOCK = threading.Lock()
PRODUCTION_READY = True


def invalidate_registry(path):
    """Revoke before a registry mutation can commit, including caller-owned transactions.

    A later rollback does not restore old authority: fresh admission is required.
    Socket writes hold the database write fence, so they serialize with this mutation.
    """
    with _FACTORY_LOCK:
        factories = tuple(_FACTORIES)
    for factory in factories:
        if factory.db_path is not None and Path(factory.db_path).resolve() == Path(path).resolve():
            factory.invalidate()


def refused() -> ProtocolError:
    return ProtocolError('Remote engine authority is missing, changed or refused.', code='protocol_dispatch_refused')


def origin(address: str) -> str:
    if (not isinstance(address, str) or any(c.isspace() or ord(c) < 32 for c in address)
            or '\\' in address or '#' in address or '?' in address):
        raise refused()
    try:
        p = urlsplit(address)
        if (p.scheme != 'https' or not p.hostname or p.username is not None or p.password is not None
                or p.fragment or p.query or '%' in p.netloc or p.hostname.endswith('.')):
            raise refused()
        port = p.port if p.port is not None else 443
        if not 0 < port < 65536:
            raise refused()
        host = p.hostname.encode('idna').decode('ascii').lower()
        if ':' in host:
            host = '[' + host + ']'
        decoded = unquote(p.path)
        if ('\\' in decoded or decoded.startswith('//') or any(s in {'.', '..'} for s in decoded.split('/'))
                or any(c.isspace() or ord(c) < 32 for c in decoded)):
            raise refused()
        return f'https://{host}:{port}'
    except (ValueError, UnicodeError):
        raise refused() from None


def policy(service: Mapping[str, Any], *, require_enabled=True) -> tuple[str, tuple[str, ...], tuple[tuple[str, float | int], ...]]:
    from gateway import service_registry as sr

    connection = connection_config(service)
    raw = connection.get('remote_dispatch')
    if (not isinstance(raw, Mapping) or type(raw.get('enabled')) is not bool
            or (require_enabled and raw.get('enabled') is not True)):
        raise refused()
    if set(raw) - {'enabled', 'version', 'private_cidrs', 'limits'} or type(raw.get('version', 1)) is not int or raw.get('version', 1) != 1:
        raise refused()
    if connection.get('dispatch') != 'protocol':
        raise refused()
    config = service.get('config') or {}
    if config.get('native_passthrough') or service.get('native_passthrough'):
        raise refused()
    if isinstance(service.get('connection'), Mapping) and isinstance(config.get('connection'), Mapping):
        if service['connection'] != config['connection']:
            raise refused()
    try:
        sr.validate_protocol_dispatch_billing(service)
    except ValueError:
        raise refused() from None
    levels = [service, config, connection, *offerings_config(service)]
    for level in levels:
        for name in ('provider_type', 'cost_class'):
            if name in level and (not isinstance(level[name], str) or level[name].strip().lower() not in sr.LOCAL_PROVIDER_CLASSES):
                raise refused()
    if not any(level.get('cost_class') == 'free-local' for level in levels):
        raise refused()
    approved = origin(connection.get('address'))
    if connection.get('control_address') and origin(connection['control_address']) != approved:
        raise refused()
    for spec in (connection.get('credentials') or {}).values():
        if spec and spec.get('secret_ref') and origin(spec.get('origin')) != approved:
            raise refused()
    exceptions = raw.get('private_cidrs', [])
    if not isinstance(exceptions, list):
        raise refused()
    parsed = []
    for text in exceptions:
        try:
            network = ipaddress.ip_network(text, strict=True)
        except (ValueError, TypeError):
            raise refused() from None
        minimum = 24 if network.version == 4 else 64
        if network.prefixlen < minimum or not network.is_private:
            raise refused()
        # Exceptions can only describe RFC1918/ULA space, never special-use ranges.
        pools = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16') if network.version == 4 else ('fc00::/7',)
        if not any(network.subnet_of(ipaddress.ip_network(pool)) for pool in pools):
            raise refused()
        parsed.append(str(network))
    catalog = yaml.safe_load((Path(__file__).resolve().parents[1] / 'registry/remote_egress.yml').read_text())['limits']
    overrides = raw.get('limits', {})
    if not isinstance(overrides, Mapping) or set(overrides) - catalog.keys():
        raise refused()
    limits = []
    for name, spec in catalog.items():
        value = overrides.get(name, spec['default'])
        if (type(value) not in (int, float) or not 0 < value <= spec['maximum'] or not math.isfinite(value)
                or (spec.get('integer') and type(value) is not int)):
            raise refused()
        limits.append((name, value))
    return approved, tuple(sorted(set(parsed))), tuple(limits)


@dataclass(frozen=True)
class Capability:
    service_id: str
    exact_origin: str
    policy_generation: str
    address_exceptions: tuple[str, ...]
    credential_refs: tuple[str, ...]
    finite_budgets: tuple[tuple[str, float | int], ...]
    admitted_at: float
    admission_id: str = field(default_factory=lambda: secrets.token_hex(16))


class ClientFactory:
    """Mint authority only from current registry data and recheck it on each send."""

    def __init__(self, registry: Callable[[], Mapping[str, Any]], *, transport=None, connector=None, db_path=None):
        self._registry = registry
        self._transport = transport
        self._connector = connector
        self._minted = weakref.WeakValueDictionary()
        self._revoked = weakref.WeakSet()
        self._budgets = weakref.WeakKeyDictionary()
        self._bindings = weakref.WeakSet()
        self.db_path = db_path
        self.clock = time.monotonic
        self._lock = threading.RLock()
        self._send_lock = asyncio.Lock()
        with _FACTORY_LOCK:
            _FACTORIES.add(self)

    def _current(self, service_id):
        snapshot = self._registry()
        registry = snapshot.get('value', snapshot)
        record = (registry.get('services') or {}).get(service_id)
        if not isinstance(record, Mapping):
            raise refused()
        if record.get('enabled', True) is False:
            raise refused()
        raw_record = ((snapshot.get('raw_value') or {}).get('services') or {}).get(service_id)
        if raw_record is not None:
            policy(raw_record)
        encoded = json.dumps([snapshot.get('operator_settings_generation', snapshot.get('generation')), record], sort_keys=True)
        return record, hashlib.sha256(encoded.encode()).hexdigest()

    def admit(self, service_id: str) -> Capability:
        record, generation = self._current(service_id)
        approved, exceptions, limits = policy(record)
        credentials = connection_config(record).get('credentials') or {}
        refs = tuple(sorted(str(row['secret_ref']) for row in credentials.values() if row and row.get('secret_ref')))
        cap = Capability(service_id, approved, generation, exceptions, refs, limits, self.clock())
        self._minted[id(cap)] = cap
        return cap

    def check(self, cap: Capability, *, service_id: str, address: str, service=None) -> None:
        if (cap in self._revoked or self._minted.get(id(cap)) is not cap or cap.service_id != service_id
                or origin(address) != cap.exact_origin):
            raise refused()
        record, generation = self._current(service_id)
        policy(record)
        if generation != cap.policy_generation or (service is not None and service != record):
            raise refused()

    def transport(self, cap: Capability):
        self.check(cap, service_id=cap.service_id, address=cap.exact_origin)
        return BoundTransport(self, cap, self._transport)

    def invalidate(self):
        with self._lock:
            self._revoked.update(tuple(self._minted.values()))
            bindings = tuple(self._bindings)
        for bound in bindings:
            bound.revoke()


class BoundTransport:
    def __init__(self, factory, capability, inner):
        self.factory, self.capability, self.inner = factory, capability, inner
        from gateway.remote_engine_budget import OperationBudget
        self.budget = factory._budgets.setdefault(capability, OperationBudget(capability, factory.clock))
        self.responses, self.clients = set(), set()
        self.tasks = {}
        self.loop = None
        self.monitor = None
        self.revoked = False
        self.capacity = None
        with factory._lock:
            factory._bindings.add(self)
        if self.inner is None:
            from gateway.remote_engine_transport import RemoteNetworkTransport
            self.inner = factory._connector(self) if factory._connector else RemoteNetworkTransport(self)

    def check(self):
        if self.revoked:
            raise asyncio.CancelledError('Remote engine authority was revoked.')
        try:
            self.factory.check(self.capability, service_id=self.capability.service_id, address=self.capability.exact_origin)
        except ProtocolError:
            self.revoke()
            raise asyncio.CancelledError('Remote engine authority was revoked.') from None
        self.budget.remaining()

    def revoke(self):
        self.revoked = True
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._cancel)

    def _cancel(self):
        for task in tuple(self.tasks):
            if not task.done():
                task.cancel('Remote engine authority was revoked.')
        self.loop.create_task(self.close())

    async def close(self):
        await asyncio.gather(*(response.aclose() for response in tuple(self.responses)), return_exceptions=True)
        await asyncio.gather(*(client.aclose() for client in tuple(self.clients)), return_exceptions=True)

    async def _watch(self):
        try:
            while self.tasks or self.responses or self.clients:
                await asyncio.sleep(min(self.budget.limits['authority_check_s'], self.budget.remaining()))
                self.check()
        except ProtocolError:
            self.revoke()
        finally:
            if self.revoked:
                await self.close()

    @asynccontextmanager
    async def operation(self):
        self.check()
        self.loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        self.tasks[task] = self.tasks.get(task, 0) + 1
        if self.monitor is None or self.monitor.done():
            self.monitor = self.loop.create_task(self._watch())
        try:
            async with asyncio.timeout(self.budget.remaining()):
                yield
        except BaseException:
            await self.close()
            raise
        finally:
            self.tasks[task] -= 1
            if not self.tasks[task]:
                del self.tasks[task]

    @asynccontextmanager
    async def fence(self):
        if self.factory.db_path is None:
            async with self.factory._send_lock:
                self.check()
                yield
            return
        from gateway.sqlite_startup import connect
        def acquire():
            conn = connect(self.factory.db_path, timeout=min(self.budget.remaining(), self.budget.limits['connect_s']),
                           check_same_thread=False)
            try:
                conn.execute('BEGIN IMMEDIATE')
            except BaseException:
                conn.close()
                raise
            return conn
        pending = asyncio.create_task(asyncio.to_thread(acquire))
        try:
            conn = await asyncio.shield(pending)
        except asyncio.CancelledError:
            conn = await pending
            conn.close()
            raise
        try:
            self.check()
            yield
        finally:
            conn.rollback()
            conn.close()

    async def send(self, request, timeouts):
        from gateway.remote_engine_budget import BoundResponse, service_capacity
        cap = self.capability
        self.factory.check(cap, service_id=cap.service_id, address=request.url)
        record, _ = self.factory._current(cap.service_id)
        names = {'accept', 'content-type', 'accept-encoding'}
        names.update(str(spec.get('header') or 'Authorization').lower()
                     for spec in (connection_config(record).get('credentials') or {}).values() if spec)
        if any(name.lower() not in names for name in request.headers):
            raise refused()
        async with self.operation():
            if self.capacity is None:
                self.capacity = service_capacity(self)
            async with asyncio.timeout(self.budget.remaining()):
                await self.capacity.acquire(self.budget.limits['connections'])
            try:
                response = await self.inner.send(request, timeouts)
                return BoundResponse(response, self)
            except BaseException:
                await self.capacity.release()
                raise


def factory_from_store():
    """Use a fresh authoritative snapshot, independent of request connection caches."""
    from gateway import admin_store, request_connections, secret_refs, service_registry
    path = secret_refs.database()
    def read():
        with request_connections.request_scope():
            snapshot = admin_store.get_service_registry_with_generation(path=path)
        if snapshot is None:
            raise refused()
        raw = snapshot['value']
        snapshot['raw_value'] = raw
        snapshot['value'] = service_registry.read_registry(raw)[0]
        return snapshot
    return ClientFactory(read, db_path=path)

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
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

import yaml

from gateway.engine_contract_view import connection_config, offerings_config
from gateway.engine_protocol.errors import ProtocolError

_FACTORIES = weakref.WeakSet()
_FACTORY_LOCK = threading.Lock()
PRODUCTION_READY = True
# Reads of the registry before an engine I/O whose snapshot a concurrent commit overtook; then the I/O is refused.
MAX_AUTHORITY_REFRESHES = 3


def invalidate_registry(path, before=None, after=None):
    """Revoke before a registry mutation can commit, including caller-owned transactions.

    A later rollback does not restore old authority: fresh admission is required.
    Active tasks are cancelled; the next I/O rechecks authority.
    """
    with _FACTORY_LOCK:
        factories = tuple(_FACTORIES)
    for factory in factories:
        if factory.db_path is not None and Path(factory.db_path).resolve() == Path(path).resolve():
            changed = None
            if before is not None and after is not None:
                old, new = before.get('services') or {}, after.get('services') or {}
                changed = {sid for sid in set(old) | set(new)
                           if authority_record(old.get(sid)) != authority_record(new.get(sid))}
            factory.invalidate(changed)


def refused() -> ProtocolError:
    return ProtocolError('Remote engine authority is missing, changed or refused.', code='protocol_dispatch_refused')


class PlainRefusal(ProtocolError):
    """A refusal the owner can act on: its message is plain words and is safe to show at save."""

    def __init__(self, message: str):
        super().__init__(message, code='protocol_dispatch_refused')


def refuse_unbound(service: Mapping[str, Any]) -> None:
    """Legacy clients have no remote authority, even for owner-enabled records."""
    if remote_enabled(connection_config(service)):
        raise refused()


def remote_enabled(connection: Mapping[str, Any]) -> bool:
    if 'remote_dispatch' not in connection:
        return False
    raw = connection['remote_dispatch']
    if not isinstance(raw, Mapping) or type(raw.get('enabled')) is not bool:
        raise refused()
    return raw['enabled']


def refuse_unbound_connection(connection) -> None:
    from gateway.engine_egress import is_remote_destination
    if (getattr(connection, 'remote_capability', None) is not None
            or getattr(connection, 'remote_enabled', False) or is_remote_destination(connection.address)):
        raise refused()


def origin(address: str) -> str:
    """The exact origin of an engine address. Plain http parses here; ``policy`` allows it only as ``private_plaintext``."""
    if (not isinstance(address, str) or any(c.isspace() or ord(c) < 32 for c in address)
            or '\\' in address or '#' in address or '?' in address):
        raise refused()
    try:
        p = urlsplit(address)
        if (p.scheme not in ('https', 'http') or not p.hostname or p.username is not None or p.password is not None
                or p.fragment or p.query or '%' in p.netloc or p.hostname.endswith('.')):
            raise refused()
        port = p.port if p.port is not None else (443 if p.scheme == 'https' else 80)
        if not 0 < port < 65536:
            raise refused()
        host = p.hostname.encode('idna').decode('ascii').lower()
        if ':' in host:
            host = '[' + host + ']'
        decoded = unquote(p.path)
        if ('\\' in decoded or decoded.startswith('//') or any(s in {'.', '..'} for s in decoded.split('/'))
                or any(c.isspace() or ord(c) < 32 for c in decoded)):
            raise refused()
        return f'{p.scheme}://{host}:{port}'
    except (ValueError, UnicodeError):
        raise refused() from None


def authority_record(record):
    """Only inputs that select an engine, credentials, billing or remote authority."""
    if not isinstance(record, Mapping):
        return None
    keys = {'id', 'enabled', 'connection', 'offerings', 'process', 'observations',
            'residency', 'protocol_operations', 'control_file_roots', 'native_passthrough',
            'provider_type', 'cost_class', 'paid_remote', 'remote_provider', 'locality',
            'placement', 'execution_substrate', 'kind', 'litellm_provider', 'provider_adapter'}
    config = record.get('config')
    config = config if isinstance(config, Mapping) else {}
    return [{key: record[key] for key in keys if key in record},
            {key: config[key] for key in keys if key in config}]


TRANSPORTS = ('tls', 'private_plaintext')
_KEY_FIELDS = ('api_key_secret_id', 'api_key_env', 'api_key_ref')


def _require_plaintext_is_safe(service, connection, approved, cidrs):
    """Plain http crosses the network in clear text, so it is allowed only where nothing secret travels it.

    The rules: an approved private network is named, no credential is bound to the record, and an address given as
    numbers is inside that network. Names are checked against the same network at every connection.
    """
    if not cidrs:
        raise PlainRefusal('Plain http needs the private network the server is on. None was given.')
    config = service.get('config') if isinstance(service.get('config'), Mapping) else {}
    carriers = (service, config, connection)
    if (any(spec and spec.get('secret_ref') for spec in (connection.get('credentials') or {}).values())
            or any(carrier.get(name) for carrier in carriers for name in _KEY_FIELDS)
            or any(carrier.get('secret_refs') for carrier in (service, config))):
        raise PlainRefusal('A key cannot be sent over plain http, even on your own network. '
                           'Put the server behind a reverse proxy that uses https, or remove the key.')
    try:
        literal = ipaddress.ip_address(urlsplit(approved).hostname.strip('[]'))
    except ValueError:
        return
    if not any(literal.version == ipaddress.ip_network(c).version and literal in ipaddress.ip_network(c) for c in cidrs):
        raise PlainRefusal('The address is not inside the private network that was approved for it.')


def network_around(addresses) -> str:
    """The private network an owner is asked to approve for these addresses: the /24 of an IPv4 one, the /64 of an IPv6 one.

    Every address must sit in that one network and in private space. Anything else is refused in plain words, so
    that approving a network can never approve a public host, this computer, a link-local or a metadata address.
    """
    found = [ipaddress.ip_address(str(a)) for a in addresses]
    if not found:
        raise PlainRefusal('That name does not point to an address.')
    first = found[0]
    network = ipaddress.ip_network(f'{first}/{24 if first.version == 4 else 64}', strict=False)
    pools = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16') if first.version == 4 else ('fc00::/7',)
    if not any(network.subnet_of(ipaddress.ip_network(pool)) for pool in pools):
        raise PlainRefusal('That address is not on a private network (10.x.x.x, 172.16 to 172.31, 192.168.x.x, or fd00::/8). '
                           'Azizam does not call a server on a public address without https.')
    if any(a.version != first.version or a not in network for a in found):
        raise PlainRefusal('That name points to addresses on more than one network. Use the server\'s own address.')
    return str(network)


@lru_cache(maxsize=1)
def limit_catalog():
    return yaml.safe_load((Path(__file__).resolve().parents[1] / 'registry/remote_egress.yml').read_text())['limits']


def policy(service: Mapping[str, Any], *, require_enabled=True) -> tuple[str, tuple[str, ...], tuple[tuple[str, float | int], ...]]:
    from gateway import service_registry as sr

    connection = connection_config(service)
    raw = connection.get('remote_dispatch')
    if (not isinstance(raw, Mapping) or type(raw.get('enabled')) is not bool
            or (require_enabled and raw.get('enabled') is not True)):
        raise refused()
    if (set(raw) - {'enabled', 'version', 'private_cidrs', 'limits', 'transport', 'tasks'}
            or type(raw.get('version', 1)) is not int or raw.get('version', 1) != 1):
        raise refused()
    transport = raw.get('transport', 'tls')
    if transport not in TRANSPORTS:
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
    if approved.startswith('http://') != (transport == 'private_plaintext'):
        # Plain http is a named choice, never a side effect of an address, and a named choice needs its own address.
        raise PlainRefusal('Plain http needs the private network choice, and a private network choice needs an http address.'
                           if transport == 'private_plaintext' else
                           'An http address needs the choice that this network is trusted. Use https for anything else.')
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
    if transport == 'private_plaintext':
        _require_plaintext_is_safe(service, connection, approved, parsed)
    tasks = raw.get('tasks')
    if tasks is not None:
        if not isinstance(tasks, list) or not tasks or not all(isinstance(task, str) and task for task in tasks):
            raise refused()
        for offering in offerings_config(service):
            for row in offering.get('tasks') or ():
                name = row.get('task') if isinstance(row, Mapping) else row
                if name not in tasks:
                    raise PlainRefusal(f'This engine may be used on another computer for {", ".join(sorted(tasks))} only, '
                                       f'and one of its offerings is for {name}.')
    catalog = limit_catalog()
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

    def __init__(self, registry: Callable[[], Mapping[str, Any]], *, transport=None, connector=None, db_path=None, registry_version=None):
        self._registry = registry
        self._transport = transport
        self._connector = connector
        self._minted = weakref.WeakValueDictionary()
        self._revoked = weakref.WeakSet()
        self._budgets = weakref.WeakKeyDictionary()
        self._operation_slots = weakref.WeakKeyDictionary()
        self._bindings = weakref.WeakSet()
        self.db_path = db_path
        self.clock = time.monotonic
        self._lock = threading.RLock()
        self._current_cache = {}
        self._policy_cache = {}
        self._registry_version = registry_version
        self._service_checks = weakref.WeakKeyDictionary()
        with _FACTORY_LOCK:
            _FACTORIES.add(self)

    def _current(self, service_id):
        with self._lock:
            # Capture before reading: a concurrent commit must remain visible to
            # the next I/O, even if it lands while this snapshot is being parsed.
            version = self._registry_version.version() if self._registry_version is not None else None
            snapshot = self._registry()
            revision = snapshot.get('operator_settings_generation', snapshot.get('generation'))
            # A revision counts within one database file: a replaced file (a new epoch) may reuse the number.
            if revision is not None and version is not None:
                revision = (version[0], revision)
            cached = self._current_cache.get(service_id)
            if revision is not None and cached is not None and cached[0] == revision:
                self._current_cache[service_id] = (*cached[:3], version)
                return cached[1], cached[2]
            registry = snapshot.get('value', snapshot)
            record = (registry.get('services') or {}).get(service_id)
            if not isinstance(record, Mapping) or record.get('enabled', True) is False:
                raise refused()
            raw = ((snapshot.get('raw_value') or {}).get('services') or {}).get(service_id)
            encoded = json.dumps([authority_record(record), authority_record(raw)], sort_keys=True)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            # Unversioned test sources may mutate in place; their digest is the revision.
            if cached is None or cached[2] != digest:
                if raw is not None:
                    policy(raw)
                self._policy_cache[service_id] = policy(record)
            self._current_cache[service_id] = (revision, record, digest, version)
            return record, digest

    def admit(self, service_id: str) -> Capability:
        record, generation = self._current(service_id)
        approved, exceptions, limits = self._policy_cache[service_id]
        credentials = connection_config(record).get('credentials') or {}
        refs = tuple(sorted(str(row['secret_ref']) for row in credentials.values() if row and row.get('secret_ref')))
        cap = Capability(service_id, approved, generation, exceptions, refs, limits, self.clock())
        self._minted[id(cap)] = cap
        return cap

    def _check_identity(self, cap, service_id, address):
        if (cap in self._revoked or self._minted.get(id(cap)) is not cap or cap.service_id != service_id
                or origin(address) != cap.exact_origin):
            raise refused()

    def check(self, cap: Capability, *, service_id: str, address: str, service=None) -> None:
        self._check_identity(cap, service_id, address)
        record, generation = self._current(service_id)
        if generation != cap.policy_generation or (service is not None and service != record):
            raise refused()

    async def check_async(self, cap, *, service_id, address):
        if self._registry_version is None:
            # Injected sources without a commit token must still be read freshly.
            await asyncio.to_thread(self.check, cap, service_id=service_id, address=address)
            return
        locks = self._service_checks.setdefault(asyncio.get_running_loop(), {})
        async with locks.setdefault(service_id, asyncio.Lock()):
            try:
                self._check_identity(cap, service_id, address)
                version = self._registry_version.version()
                cached = self._current_cache.get(service_id)
                refreshes = 0
                while cached is None or cached[3] != version:
                    # Never authorize I/O from a snapshot overtaken during refresh: a commit while it was read (any
                    # admin.db commit moves the version) means read again. A registry that never settles is refused.
                    if refreshes == MAX_AUTHORITY_REFRESHES:
                        raise refused()
                    refreshes += 1
                    await asyncio.to_thread(self.check, cap, service_id=service_id, address=address)
                    version = self._registry_version.version()
                    cached = self._current_cache.get(service_id)
                if cached[2] != cap.policy_generation:
                    raise refused()
                self._check_identity(cap, service_id, address)
            except Exception:
                raise refused() from None

    def transport(self, cap: Capability):
        self.check(cap, service_id=cap.service_id, address=cap.exact_origin)
        return BoundTransport(self, cap, self._transport)

    def invalidate(self, service_ids=None):
        with self._lock:
            self._revoked.update(cap for cap in tuple(self._minted.values())
                                 if service_ids is None or cap.service_id in service_ids)
            bindings = tuple(bound for bound in self._bindings
                             if service_ids is None or bound.capability.service_id in service_ids)
        for bound in bindings:
            bound.revoke()


class BoundTransport:
    def __init__(self, factory, capability, inner):
        self.factory, self.capability, self.inner = factory, capability, inner
        from gateway.remote_engine_budget import OperationBudget, OperationSlot
        self.budget = factory._budgets.setdefault(capability, OperationBudget(capability, factory.clock))
        self.operation_slot = factory._operation_slots.setdefault(capability, OperationSlot())
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
        self.budget.remaining()

    async def check_current(self):
        self.check()
        try:
            await self.factory.check_async(self.capability,
                                           service_id=self.capability.service_id, address=self.capability.exact_origin)
        except ProtocolError:
            self.revoke()
            raise asyncio.CancelledError('Remote engine authority was revoked.') from None
        # Revocation can arrive while the worker is checking the committed generation.
        self.check()

    def revoke(self):
        if self.revoked:
            return
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

    @asynccontextmanager
    async def operation(self):
        from gateway.remote_engine_watch import watch
        self.operation_slot.enter(self)
        try:
            await self.check_current()
            self.loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            self.tasks[task] = self.tasks.get(task, 0) + 1
            self.monitor = watch(self)
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
        finally:
            self.operation_slot.leave()

    @asynccontextmanager
    async def fence(self):
        # A check authorizes the next I/O only. Never hold a database transaction
        # or a writer lock while the peer controls completion of that operation.
        await self.check_current()
        yield
        await self.check_current()

    async def send(self, request, timeouts):
        from gateway.remote_engine_budget import BoundResponse, service_capacity
        cap = self.capability
        await asyncio.to_thread(self.factory.check, cap, service_id=cap.service_id, address=request.url)
        record, _ = await asyncio.to_thread(self.factory._current, cap.service_id)
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
    from gateway.registry_version import RegistryVersion
    path = secret_refs.database()
    reader = RegistryVersion(path)
    cached = None
    cached_version = None
    def read():
        nonlocal cached, cached_version
        version = reader.version()
        if cached is not None and version == cached_version:
            return cached
        if cached is not None and version[0] != cached_version[0]:
            # A replaced database file: its revision number says nothing about the old file's snapshot.
            cached = None
        with request_connections.request_scope():
            revision = admin_store.service_registry_generation(path=path)
            if cached is not None and cached['operator_settings_generation'] == revision:
                cached_version = version
                return cached
            encoded = admin_store.get_service_registry_json_with_generation(path=path)
        if encoded is None:
            raise refused()
        revision = encoded['operator_settings_generation']
        if cached is None or cached['operator_settings_generation'] != revision:
            from gateway import registry_layers
            raw = registry_layers.layer(json.loads(encoded['value_json']), service_registry.release_registry())
            cached = {'raw_value': raw, 'value': service_registry.read_registry(raw)[0],
                      'operator_settings_generation': revision}
        cached_version = version
        return cached
    return ClientFactory(read, db_path=path, registry_version=reader)

"""The one place an HTTP request to an engine is checked against a serving lease, at send time.

Every httpx client the gateway opens comes from ``async_client`` or ``client`` here. Each carries a request hook
that runs for every request the client sends, including each redirect hop and every request a long-lived client
sends later. The hook looks up the destination ``host:port`` in a map of the transition-enabled residency domains,
projected from the registry each time the runtime config is published (``publish``). A destination in that map
needs the sending task to hold an unreleased lease for the domain (``transition_gate.holds_lease``) or to be inside
``permit_domain`` for it (the transition executor's control calls only); otherwise
the hook raises ``TransitionUnavailable`` before the transport sees the request, so no byte leaves.

The per-route guards (``_require_egress_lease`` in proxy.py and the protocol hops) stay as the second layer: they
refuse earlier, with the route's own context, and they cover a connection that is not an httpx client. A
websocket is not an httpx request: the hook cannot see its send, so the websocket sites keep an explicit guard
(tests/test_engine_egress_choke_point.py lists them).

A probe is a read of health or metadata. It marks its own request with ``probe()`` (a request extension the hook
removes before the transport sees it). The mark counts only on a GET, and only when the path is one the service
record declares as a health, metadata or observer path (``declared_probe_paths``), whole and equal to the request
path (or that path under the path prefix of the record's own URL): some engines change state on a GET
(llama-swap ``GET /unload``, ``GET /upstream/<model>/...``), so a GET of any other path to an enabled domain needs
a lease. Nothing else is exempt, and no function name is.

The destination map is host and port only: two names for one address are two destinations, and an engine
reached by a name that is not the one in its record is not mapped. The gateway builds every engine URL from
the record, so the names agree.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any
from pathlib import Path
import threading
import asyncio
import weakref
import json
from urllib.parse import urlsplit

import httpx

from gateway.transition_gate import TransitionUnavailable, holds_lease, policy_for

PROBE_EXTENSION = "azizam_egress_probe"


def probe() -> dict[str, Any]:
    """The request extensions of a probe: pass as ``extensions=engine_egress.probe()`` on a GET that only reads
    health or metadata. A new dict each call, because the hook removes the mark from the request's own dict."""
    return {PROBE_EXTENSION: True}


_PERMITTED: ContextVar[frozenset[str]] = ContextVar("engine_egress_permitted_domains", default=frozenset())


@contextmanager
def permit_domain(domain: str) -> Iterator[None]:
    """Let the current task send to ``domain``'s engines without a serving lease, for the ``with`` block only.

    For the transition executor's own control calls (release, load, query, probes): they run while the gate is
    closed and the executor holds the domain lock, and a serving lease would count against the drain that precedes
    them. The permit is scoped to one domain by name; a task spawned inside the block inherits it, one spawned
    outside never does, and a send to any other enabled domain is still refused.
    """
    token = _PERMITTED.set(_PERMITTED.get() | {domain})
    try:
        yield
    finally:
        _PERMITTED.reset(token)


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_LOOPBACK = {"localhost": "127.0.0.1", "::1": "127.0.0.1", "[::1]": "127.0.0.1"}
_URL_FIELDS = ("url", "base_url", "api_base", "default_base_url", "address", "upstream")

# The path a service reads for health when its record names none.
DEFAULT_HEALTH_PATH = "/health"


def destination_key(host: str, port: int | None, scheme: str = "http") -> str:
    name = _LOOPBACK.get(host.strip().lower(), host.strip().lower())
    return f"{name}:{port if port else _DEFAULT_PORTS.get(scheme.lower(), 80)}"


def _key_of_url(value: Any) -> str | None:
    if not isinstance(value, str) or "://" not in value:
        return None
    try:
        parsed = urlsplit(value.strip())
        if not parsed.hostname:
            return None
        return destination_key(parsed.hostname, parsed.port, parsed.scheme)
    except ValueError:
        return None


def _prefix_of_url(value: Any) -> str:
    """The path of a record's URL with no trailing slash (``/v1`` for ``http://h:1/v1/``); empty when it has none."""
    try:
        return urlsplit(value.strip()).path.rstrip("/")
    except (AttributeError, ValueError):
        return ""


def _destination_prefixes(record: Mapping[str, Any]) -> dict[str, set[str]]:
    """The ``host:port`` keys one service record is reached at, each with the URL path prefixes the record gives it."""
    from gateway.service_registry import environment_base_url  # service_registry imports this module

    found: dict[str, set[str]] = {}
    scopes = [record]
    if isinstance(record.get("config"), Mapping):
        scopes.append(record["config"])
    for scope in scopes:
        host, port = scope.get("host"), scope.get("port")
        if isinstance(host, str) and host.strip():
            if "://" in host:
                key = _key_of_url(host)
                if key:
                    found.setdefault(key, set()).add(_prefix_of_url(host))
            elif isinstance(port, (int, str)) and not isinstance(port, bool) and str(port).isdigit():
                found.setdefault(destination_key(host, int(port), str(scope.get("scheme") or "http")), set()).add("")
        # The address a record's ``base_url_env`` variable holds is dialed in place of the record's own, so the gate
        # maps it too: a lease for the record's domain covers both.
        urls = [*(scope.get(field) for field in _URL_FIELDS), environment_base_url(scope)]
        connection = scope.get("connection")
        if isinstance(connection, Mapping):
            urls.append(connection.get("address"))
            urls.append(connection.get("control_address"))
        for url in urls:
            key = _key_of_url(url)
            if key:
                found.setdefault(key, set()).add(_prefix_of_url(url))
    return found


def destinations_of(record: Mapping[str, Any]) -> set[str]:
    """The ``host:port`` keys one service record is reached at: its host and port, and its URL fields."""
    return set(_destination_prefixes(record))


def _path_of(value: Any) -> str | None:
    """The path part of a declared endpoint: a path as written, or the path of an absolute URL."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if "://" in text:
        try:
            text = urlsplit(text).path
        except ValueError:
            return None
    text = text.split("?", 1)[0]
    return text if text.startswith("/") and text != "/" else None


def observer_paths_declared_by(services: Mapping[str, Any]) -> dict[str, set[str]]:
    """The observer paths other records name, by the id of the service they are read from.

    A record that sets ``resource_capacity.observer`` asks its observer service for facts at ``path``: that path is
    an observer read of the observer service, so its record declares it (``project`` adds it to that service's paths).
    """
    found: dict[str, set[str]] = {}
    for record in services.values():
        if not isinstance(record, Mapping):
            continue
        for scope in (record, record.get("config")):
            capacity = scope.get("resource_capacity") if isinstance(scope, Mapping) else None
            observer = capacity.get("observer") if isinstance(capacity, Mapping) else None
            if not isinstance(observer, Mapping) or not isinstance(observer.get("service"), str):
                continue
            path = _path_of(observer.get("path"))
            if path:
                found.setdefault(observer["service"].strip(), set()).add(path)
    return found


def declared_probe_paths(record: Mapping[str, Any]) -> set[str]:
    """Every path a service record declares as a health, metadata or observer read: ``relative | server-root``.

    See ``split_declared_probe_paths`` for the two kinds; ``project`` expands only the first under a path prefix.
    """
    relative, rooted = split_declared_probe_paths(record)
    return relative | rooted


def split_declared_probe_paths(record: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """The paths a service record declares as health, metadata or observer reads (the only paths a probe mark covers).

    These are the record's ``health_endpoint`` (and the default health path), the readiness read its definition declares, its ``context_window_probe`` endpoint,
    the path of each declared observation, the ``verify_endpoint`` of its lifecycle driver, and the same fields of
    each offering. (The observer path another record names for this service is added by ``project``.)
    The first set holds paths written relative to the record's URL, which ``project`` allows as written and under the
    path prefix of the record's own URL. The second set holds the definition's readiness path, written from the
    server root: ``project`` allows it exactly as written, never under a prefix.
    """
    found: set[str] = {DEFAULT_HEALTH_PATH}
    rooted: set[str] = set()
    scopes: list[Mapping[str, Any]] = [record]
    if isinstance(record.get("config"), Mapping):
        scopes.append(record["config"])
    for scope in list(scopes):
        offerings = scope.get("offerings")
        if isinstance(offerings, list):
            scopes.extend(item for item in offerings if isinstance(item, Mapping))
    if isinstance(record.get("config"), Mapping):
        # The readiness read the record's definition declares: an older record with no health endpoint falls back to it.
        from gateway.engine_readiness import record_readiness_path

        declared = record_readiness_path(record["config"])
        if declared:
            rooted.add(declared)
    for scope in scopes:
        for value in (scope.get("health_endpoint"),
                      (scope.get("context_window_probe") or {}).get("endpoint") if isinstance(scope.get("context_window_probe"), Mapping) else None):
            path = _path_of(value)
            if path:
                found.add(path)
        observations = scope.get("observations")
        if isinstance(observations, Mapping):
            for spec in observations.values():
                http = spec.get("http") if isinstance(spec, Mapping) else None
                path = _path_of(http.get("path")) if isinstance(http, Mapping) else None
                if path:
                    found.add(path)
        lifecycle = scope.get("lifecycle")
        driver_config = lifecycle.get("driver_config") if isinstance(lifecycle, Mapping) else None
        if isinstance(driver_config, Mapping):
            for key in ("health_endpoint", "verify_endpoint"):
                path = _path_of(driver_config.get(key))
                if path:
                    found.add(path)
    return found - rooted, rooted


class Projection:
    """The destination map and the probe paths, built from one set of services and not yet live (see ``project``).

    ``destinations`` maps each transition-enabled address to its domains. ``probe_paths`` maps each address to the
    exact request paths a probe mark covers: each path a record declares (see ``declared_probe_paths``), alone and
    under the path prefix of the record's own URL. Both maps are immutable and belong together: a reader takes the
    whole projection once and never combines the maps of two projections.
    """

    __slots__ = ("destinations", "probe_paths", "remote_destinations", "registry_path", "registry_generation", "service_destinations", "registry_reader", "registry_version")

    def __init__(self, destinations: Mapping[str, tuple[str, ...]], probe_paths: Mapping[str, frozenset[str]], remote_destinations: frozenset[str] = frozenset(), service_destinations=None) -> None:
        self.destinations = destinations
        self.probe_paths = probe_paths
        self.remote_destinations = remote_destinations
        self.registry_path = None
        self.registry_generation = None
        self.registry_reader = None
        self.registry_version = None
        self.service_destinations = service_destinations or MappingProxyType({})


# The live projection: one reference, replaced whole by ``install``. Read it once per check.
_live: Projection = Projection(MappingProxyType({}), MappingProxyType({}))
_LIVE_LOCK = threading.RLock()


def project(services: Mapping[str, Any] | None, *, extra_probe_paths: Callable[[Mapping[str, Any]], Iterable[str]] | None = None) -> Projection:
    """Build the map from these services without making it live, so a failure here changes nothing.

    ``extra_probe_paths`` names further metadata paths a record reads (its ``loaded_model_probe`` endpoint, or the
    default one): the caller knows the probe fields, not this module.

    Warning: a path that is listed as a probe path is sent without a lease. The paths are exact so that a health
    check never wakes an engine. A path whose mere access starts or loads a model, such as the base URL of a backend
    that loads on first request, or an unload or upstream-forward path, must not be a ``health_endpoint``, an
    observation path or an extra probe path. Without a lease, such a probe would load a model that the transition
    gate had not admitted.
    """
    built: dict[str, list[str]] = {}
    paths: dict[str, set[str]] = {}
    remote_destinations = set()
    service_destinations = {}
    observer_paths = observer_paths_declared_by(services or {})
    for service_id, record in (services or {}).items():
        if not isinstance(record, Mapping):
            continue
        service_destinations[str(service_id)] = frozenset(destinations_of(record))
        from gateway.engine_contract_view import connection_config
        from gateway.remote_engine import remote_enabled
        if remote_enabled(connection_config(record)):
            remote_destinations.update(destinations_of(record))
        policy = policy_for(str(service_id), services)
        if not policy.enabled:
            continue
        declared, rooted = split_declared_probe_paths(record)
        declared = declared | observer_paths.get(str(service_id), set())
        if extra_probe_paths is not None:
            declared.update(path for path in map(_path_of, extra_probe_paths(record)) if path)
        for key, prefixes in _destination_prefixes(record).items():
            if policy.domain not in built.setdefault(key, []):
                built[key].append(policy.domain)
            paths.setdefault(key, set()).update(prefix + path for prefix in prefixes | {""} for path in declared)
            paths[key].update(rooted)
    return Projection(MappingProxyType({key: tuple(domains) for key, domains in built.items()}),
                      MappingProxyType({key: frozenset(value) for key, value in paths.items()}), frozenset(remote_destinations), MappingProxyType(service_destinations))


def install(projection: Projection, *, registry_path=None, registry_generation=None) -> None:
    """Publish the projection before its config; keep legacy remote denials until restart."""
    global _live
    with _LIVE_LOCK:
        projection.registry_path = str(Path(registry_path).resolve()) if registry_path is not None else None
        projection.registry_generation = registry_generation
        if projection.registry_path is not None and projection.registry_path == _live.registry_path:
            projection.remote_destinations = projection.remote_destinations | _live.remote_destinations
            projection.registry_reader = _live.registry_reader
            projection.registry_version = _live.registry_version
        elif projection.registry_path is not None:
            from gateway import admin_store
            from gateway.registry_version import RegistryVersion
            projection.registry_reader = RegistryVersion(projection.registry_path)
            try:
                version = projection.registry_reader.version()
                if admin_store.service_registry_generation(path=Path(projection.registry_path)) == registry_generation:
                    projection.registry_version = version
            except Exception:
                pass  # retain known denials; a store outage must not disable local engines
        _live = projection


def protect_registry_write(path, services) -> None:
    """Fail closed before an active registry write commits, including a failed reload/rollback."""
    with _LIVE_LOCK:
        live = _live
        if live.registry_path != str(Path(path).resolve()):
            return  # another database, not the registry this process serves
        from gateway.engine_contract_view import connection_config
        remote = set(live.remote_destinations)
        for service_id, record in (services or {}).items():
            from gateway.remote_engine import remote_enabled
            if isinstance(record, Mapping) and remote_enabled(connection_config(record)):
                remote.update(destinations_of(record))
                remote.update(live.service_destinations.get(str(service_id), ()))
        install(Projection(live.destinations, live.probe_paths, frozenset(remote), live.service_destinations), registry_path=path,
                registry_generation=live.registry_generation)


def _changed_registry_version():
    live = _live
    if live.registry_reader is not None:
        try:
            version = live.registry_reader.version()
            if version != live.registry_version:
                return version
        except Exception:
            pass  # preserve the last known denial set, including during store outages
    return None


def refresh_remote_destinations() -> None:
    """Observe CLI writes before a legacy send, without relying on reload callbacks."""
    live = _live
    version = _changed_registry_version()
    if version is None:
        return
    from gateway import admin_store, request_connections, registry_layers, service_registry
    from gateway.remote_engine import refused
    path = Path(live.registry_path)
    try:
        with request_connections.request_scope():
            revision = admin_store.service_registry_generation(path=path)
            if revision == live.registry_generation:
                with _LIVE_LOCK:
                    if _live is live:
                        live.registry_version = version
                return
            encoded = admin_store.get_service_registry_json_with_generation(path=path)
        if encoded is None:
            raise refused()
        raw = registry_layers.layer(json.loads(encoded['value_json']), service_registry.release_registry())
        with _LIVE_LOCK:
            if _live.registry_path == live.registry_path:
                protect_registry_write(path, raw.get('services'))
                current = _live
                install(Projection(current.destinations, current.probe_paths, current.remote_destinations, current.service_destinations),
                        registry_path=path, registry_generation=encoded['operator_settings_generation'])
                _live.registry_version = version
    except Exception:
        pass  # never discard known denials or turn a registry outage into a local outage


async def refresh_remote_destinations_async() -> None:
    if _changed_registry_version() is not None:
        await asyncio.to_thread(refresh_remote_destinations)


def publish(services: Mapping[str, Any] | None, *, extra_probe_paths: Callable[[Mapping[str, Any]], Iterable[str]] | None = None) -> None:
    """Replace the map from these services: ``project`` then ``install``."""
    install(project(services, extra_probe_paths=extra_probe_paths))


def mapped_destinations() -> Mapping[str, tuple[str, ...]]:
    return _live.destinations


def is_remote_destination(address: str) -> bool:
    """Also guard legacy connections constructed without their service marker."""
    return _key_of_url(address) in _live.remote_destinations


def check_request(request: httpx.Request, *, remote_bound=None, refreshed=False) -> None:
    """Refuse a request to a transition-enabled domain's address unless the task holds a lease for it."""
    if not refreshed:
        refresh_remote_destinations()
    marked = request.extensions.pop(PROBE_EXTENSION, False) is True and request.method == "GET"
    key = destination_key(request.url.host, request.url.port, request.url.scheme)
    live = _live  # one read: the destination map and the probe paths must come from the same projection
    if key in live.remote_destinations:
        from gateway.remote_engine import refused
        if remote_bound is None:
            raise refused()
        from gateway.remote_engine import origin
        remote_bound.check()
        if origin(str(request.url)) != remote_bound.capability.exact_origin:
            raise refused()
    domains = live.destinations.get(key)
    if not domains or (marked and request.url.path in live.probe_paths.get(key, ())):
        return
    if any(holds_lease(domain) for domain in domains):
        return
    permitted = _PERMITTED.get()
    if any(domain in permitted for domain in domains):
        return
    raise TransitionUnavailable(domains[0], reason="path_not_leased")


async def _check_async(request: httpx.Request) -> None:
    await refresh_remote_destinations_async()
    check_request(request, refreshed=True)


def _guard(created: Any, hook: Any) -> Any:
    hooks = getattr(created, "event_hooks", None)
    if isinstance(hooks, dict):  # a test double that stands in for httpx has no hooks and sends nothing
        hooks.setdefault("request", []).append(hook)
    return created


# Requests sent through ``async_client`` and not yet finished, by ``host:port``. A speech server holds no serving
# lease, so this is the one signal that says it is working. A request counts from its send to the close of its response
# (the end of a stream), to its failure, or to its cancellation.
_IN_FLIGHT: dict[str, int] = {}
_IN_FLIGHT_LOCK = threading.Lock()


def _count(key: str, change: int) -> None:
    with _IN_FLIGHT_LOCK:
        _count_locked(key, change)


def _count_locked(key: str, change: int) -> None:
    """``_count`` for a caller that already holds ``_IN_FLIGHT_LOCK``."""
    left = _IN_FLIGHT.get(key, 0) + change
    if left > 0:
        _IN_FLIGHT[key] = left
    else:
        _IN_FLIGHT.pop(key, None)


# Addresses an unload is running on, each with the event that opens when it ends. A request that arrives meanwhile waits
# on the event, then runs (the engine reloads on demand); it never fails because of an unload. Guarded by the same lock
# as ``_IN_FLIGHT``, so "no request is counted" and "the address is marked" are one step, and so are "the address is
# not marked" and "this request is counted".
_UNLOADING: dict[str, asyncio.Event] = {}
class _UnloadLease:
    """One unload's claim: the exact addresses and gates ``begin_unload`` took, kept so ``end_unload`` retires them even
    if a registry update changed the service's addresses meanwhile. ``active`` turns False once, under the lock."""

    __slots__ = ("service_id", "gates", "active", "task")

    def __init__(self, service_id: str, gates: dict[str, asyncio.Event]) -> None:
        self.service_id = service_id
        self.gates = gates
        self.active = True
        # The task that took the lease. Only its own requests are the unload's; a task it starts shares the context
        # but not the lease, so that task's requests wait and count like any other.
        self.task = _current_task()


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None  # no running loop: a plain call, as in a synchronous caller


# The unloads this task holds. The unload's own request goes to an address its lease claimed; it must neither wait on
# its own gate nor count as serving. A task created meanwhile inherits this tuple, but a retired lease grants nothing,
# so a copied context cannot outlive the claim. Set by ``begin_unload``, cleared by ``end_unload``.
_UNLOAD_OWNER: ContextVar[tuple[_UnloadLease, ...]] = ContextVar("engine_egress_unload_owner", default=())


def begin_unload(service_id: str) -> bool:
    """Claim the service's addresses for an unload. False when a request is in flight there or another unload holds one.

    The check and the claim are one lock hold. Pair every True with ``end_unload`` (in a ``finally``). The calling task's
    own requests to the claimed addresses pass the gate until ``end_unload``. It needs a running task: a lease with no
    owning task could be neither used nor retired by any later task, so a plain call is refused before any gate exists.
    """
    if _current_task() is None:
        raise RuntimeError("begin_unload needs a running asyncio task; call it from the task that sends the unload")
    with _IN_FLIGHT_LOCK:
        keys = frozenset(_live.service_destinations.get(service_id, ()))
        if any(_IN_FLIGHT.get(key, 0) > 0 or key in _UNLOADING for key in keys):
            return False
        lease = _UnloadLease(service_id, {key: asyncio.Event() for key in keys})
        _UNLOADING.update(lease.gates)
    _UNLOAD_OWNER.set(_UNLOAD_OWNER.get() + (lease,))
    return True


def end_unload(service_id: str) -> None:
    """Retire this task's lease on the service: free exactly the gates it took and wake the requests that waited.

    Safe to call twice; it never touches a gate another lease holds."""
    held = _UNLOAD_OWNER.get()
    task = _current_task()
    mine = [lease for lease in held if lease.service_id == service_id and lease.task is task]
    with _IN_FLIGHT_LOCK:
        for lease in mine:
            lease.active = False
            for key, gate in lease.gates.items():
                if _UNLOADING.get(key) is gate:
                    del _UNLOADING[key]
    if mine:
        _UNLOAD_OWNER.set(tuple(lease for lease in held if lease not in mine))
    for lease in mine:
        for gate in lease.gates.values():
            gate.set()


def _owned(key: str) -> bool:
    """True when this task holds a live lease on the address (the unload's own request)."""
    held = _UNLOAD_OWNER.get()
    if not held:
        return False
    task = _current_task()
    with _IN_FLIGHT_LOCK:
        return any(lease.active and lease.task is task and key in lease.gates for lease in held)


def _enter(key: str) -> asyncio.Event | None:
    """Count a request in, or return the event of the unload it must wait for."""
    with _IN_FLIGHT_LOCK:
        gate = _UNLOADING.get(key)
        if gate is not None:
            return gate
        _IN_FLIGHT[key] = _IN_FLIGHT.get(key, 0) + 1
        return None


def in_flight(service_id: str) -> int:
    """Requests sent to any address the service is reached at that have not finished. Health and metadata probes do not count."""
    with _IN_FLIGHT_LOCK:
        return sum(_IN_FLIGHT.get(key, 0) for key in _live.service_destinations.get(service_id, ()))


class _EndsOnce(httpx.AsyncByteStream):
    """The response body, whose close also ends the request's count (once, however often it is closed)."""

    def __init__(self, stream: Any, ended: Callable[[], None]) -> None:
        self._stream = stream
        self._ended = ended

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            self._ended()


def _counted(created: Any) -> Any:
    """Count each request ``created`` sends (see ``_IN_FLIGHT``). A stand-in for httpx that is not a client is left alone."""
    send = getattr(created, "send", None)
    if not isinstance(getattr(created, "event_hooks", None), dict) or not callable(send):
        return created  # a test double that stands in for httpx sends nothing

    async def counted_send(request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if request.extensions.get(PROBE_EXTENSION) is True and request.method == "GET":
            return await send(request, **kwargs)  # a probe reads health or metadata; it is not work
        key = destination_key(request.url.host, request.url.port, request.url.scheme)
        if _owned(key):
            return await send(request, **kwargs)  # the unload's own request: it holds the gate and is not serving
        while (gate := _enter(key)) is not None:
            await gate.wait()
        ended = False

        def end() -> None:
            nonlocal ended
            with _IN_FLIGHT_LOCK:  # the finalizer may run on another thread; the flag and the count change together
                if ended:
                    return
                ended = True
                _count_locked(key, -1)

        try:
            response = await send(request, **kwargs)
        except BaseException:
            end()
            raise
        if response.is_closed:
            end()  # not streamed: httpx has read and closed it already
        else:
            response.stream = _EndsOnce(response.stream, end)
            # A caller that drops the open response without closing it would keep the count up for good; the finalizer
            # ends it then. ``end`` runs once, so a close and the finalizer together still count down once.
            weakref.finalize(response, end).atexit = False
        return response

    created.send = counted_send  # httpx's request, get, post and stream all go through send
    return created


def async_client(*, _remote_bound=None, **kwargs: Any) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose every request passes the lease check and is counted while it runs."""
    if _remote_bound is None:
        return _counted(_guard(httpx.AsyncClient(**kwargs), _check_async))
    async def check(request):
        await _remote_bound.check_current()
        check_request(request, remote_bound=_remote_bound, refreshed=True)
    return _counted(_guard(httpx.AsyncClient(**kwargs), check))


def client(**kwargs: Any) -> httpx.Client:
    """An ``httpx.Client`` whose every request passes the lease check."""
    return _guard(httpx.Client(**kwargs), check_request)

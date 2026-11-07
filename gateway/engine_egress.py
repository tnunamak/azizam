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
        urls = [scope.get(field) for field in _URL_FIELDS]
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
    """The paths a service record declares as health, metadata or observer reads (the only paths a probe mark covers).

    These are the record's ``health_endpoint`` (and the default health path), the readiness read its definition declares, its ``context_window_probe`` endpoint,
    the path of each declared observation, the ``verify_endpoint`` of its lifecycle driver, and the same fields of
    each offering. (The observer path another record names for this service is added by ``project``.)
    ``project`` allows each path as written and under the path prefix of the record's own URL.
    """
    found: set[str] = {DEFAULT_HEALTH_PATH}
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
            found.add(declared)
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
    return found


class Projection:
    """The destination map and the probe paths, built from one set of services and not yet live (see ``project``).

    ``destinations`` maps each transition-enabled address to its domains. ``probe_paths`` maps each address to the
    exact request paths a probe mark covers: each path a record declares (see ``declared_probe_paths``), alone and
    under the path prefix of the record's own URL. Both maps are immutable and belong together: a reader takes the
    whole projection once and never combines the maps of two projections.
    """

    __slots__ = ("destinations", "probe_paths")

    def __init__(self, destinations: Mapping[str, tuple[str, ...]], probe_paths: Mapping[str, frozenset[str]]) -> None:
        self.destinations = destinations
        self.probe_paths = probe_paths


# The live projection: one reference, replaced whole by ``install``. Read it once per check.
_live: Projection = Projection(MappingProxyType({}), MappingProxyType({}))


def project(services: Mapping[str, Any] | None, *, extra_probe_paths: Callable[[Mapping[str, Any]], Iterable[str]] | None = None) -> Projection:
    """Build the map from these services without making it live, so a failure here changes nothing.

    ``extra_probe_paths`` names further metadata paths a record's kind reads (the loaded-state endpoint of its
    ``state_type``): the engine kinds are known to the caller, not to this module.

    Warning: a path that is listed as a probe path is sent without a lease. The paths are exact so that a health
    check never wakes an engine. A path whose mere access starts or loads a model, such as the base URL of a backend
    that loads on first request, or an unload or upstream-forward path, must not be a ``health_endpoint``, an
    observation path or an extra probe path. Without a lease, such a probe would load a model that the transition
    gate had not admitted.
    """
    built: dict[str, list[str]] = {}
    paths: dict[str, set[str]] = {}
    observer_paths = observer_paths_declared_by(services or {})
    for service_id, record in (services or {}).items():
        if not isinstance(record, Mapping):
            continue
        policy = policy_for(str(service_id), services)
        if not policy.enabled:
            continue
        declared = declared_probe_paths(record) | observer_paths.get(str(service_id), set())
        if extra_probe_paths is not None:
            declared.update(path for path in map(_path_of, extra_probe_paths(record)) if path)
        for key, prefixes in _destination_prefixes(record).items():
            if policy.domain not in built.setdefault(key, []):
                built[key].append(policy.domain)
            paths.setdefault(key, set()).update(prefix + path for prefix in prefixes | {""} for path in declared)
    return Projection(MappingProxyType({key: tuple(domains) for key, domains in built.items()}),
                      MappingProxyType({key: frozenset(value) for key, value in paths.items()}))


def install(projection: Projection) -> None:
    """Make a projection live: one reference swap, so a reader sees the old pair or the new pair, never a mix."""
    global _live
    _live = projection


def publish(services: Mapping[str, Any] | None, *, extra_probe_paths: Callable[[Mapping[str, Any]], Iterable[str]] | None = None) -> None:
    """Replace the map from these services: ``project`` then ``install``."""
    install(project(services, extra_probe_paths=extra_probe_paths))


def mapped_destinations() -> Mapping[str, tuple[str, ...]]:
    return _live.destinations


def check_request(request: httpx.Request) -> None:
    """Refuse a request to a transition-enabled domain's address unless the task holds a lease for it."""
    marked = request.extensions.pop(PROBE_EXTENSION, False) is True and request.method == "GET"
    key = destination_key(request.url.host, request.url.port, request.url.scheme)
    live = _live  # one read: the destination map and the probe paths must come from the same projection
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
    check_request(request)


def _guard(created: Any, hook: Any) -> Any:
    hooks = getattr(created, "event_hooks", None)
    if isinstance(hooks, dict):  # a test double that stands in for httpx has no hooks and sends nothing
        hooks.setdefault("request", []).append(hook)
    return created


def async_client(**kwargs: Any) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose every request passes the lease check."""
    return _guard(httpx.AsyncClient(**kwargs), _check_async)


def client(**kwargs: Any) -> httpx.Client:
    """An ``httpx.Client`` whose every request passes the lease check."""
    return _guard(httpx.Client(**kwargs), check_request)

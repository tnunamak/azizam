"""The one place an HTTP request to an engine is checked against a serving lease, at send time.

Every httpx client the gateway opens comes from ``async_client`` or ``client`` here. Each carries a request hook
that runs for every request the client sends, including each redirect hop and every request a long-lived client
sends later. The hook looks up the destination ``host:port`` in a map of the transition-enabled residency domains,
projected from the registry each time the runtime config is published (``publish``). A destination in that map
needs the sending task to hold an unreleased lease for the domain (``transition_gate.holds_lease``); otherwise
the hook raises ``TransitionUnavailable`` before the transport sees the request, so no byte leaves.

The per-route guards (``_require_egress_lease`` in proxy.py and the protocol hops) stay as the second layer: they
refuse earlier, with the route's own context, and they cover a connection that is not an httpx client. A
websocket is not an httpx request: the hook cannot see its send, so the websocket sites keep an explicit guard
(tests/test_engine_egress_choke_point.py lists them).

A probe is a read of health or metadata. It marks its own request with ``probe()`` (a request extension the hook
removes before the transport sees it). The mark counts only on a GET, and only when the path is one the service
record declares as a health, metadata or observer path (``declared_probe_paths``): some engines change state on
a GET (llama-swap ``GET /unload``), so a GET of any other path to an enabled domain needs a lease. Nothing else
is exempt, and no function name is.

The destination map is host and port only: two names for one address are two destinations, and an engine
reached by a name that is not the one in its record is not mapped. The gateway builds every engine URL from
the record, so the names agree.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
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


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_LOOPBACK = {"localhost": "127.0.0.1", "::1": "127.0.0.1", "[::1]": "127.0.0.1"}
_URL_FIELDS = ("url", "base_url", "api_base", "default_base_url", "address", "upstream")

_destinations: Mapping[str, tuple[str, ...]] = MappingProxyType({})
# The health, metadata and observer paths the records declare, by destination (see ``declared_probe_paths``).
_probe_paths: Mapping[str, frozenset[str]] = MappingProxyType({})
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


def destinations_of(record: Mapping[str, Any]) -> set[str]:
    """The ``host:port`` keys one service record is reached at: its host and port, and its URL fields."""
    found: set[str] = set()
    scopes = [record]
    if isinstance(record.get("config"), Mapping):
        scopes.append(record["config"])
    for scope in scopes:
        host, port = scope.get("host"), scope.get("port")
        if isinstance(host, str) and host.strip():
            if "://" in host:
                key = _key_of_url(host)
                if key:
                    found.add(key)
            elif isinstance(port, (int, str)) and not isinstance(port, bool) and str(port).isdigit():
                found.add(destination_key(host, int(port), str(scope.get("scheme") or "http")))
        for field in _URL_FIELDS:
            key = _key_of_url(scope.get(field))
            if key:
                found.add(key)
        connection = scope.get("connection")
        if isinstance(connection, Mapping):
            key = _key_of_url(connection.get("address"))
            if key:
                found.add(key)
    return found


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


def declared_probe_paths(record: Mapping[str, Any]) -> set[str]:
    """The paths a service record declares as health, metadata or observer reads (the only paths a probe mark covers).

    These are the record's ``health_endpoint`` (and the default health path), its ``context_window_probe`` endpoint,
    the path of each declared observation, the ``verify_endpoint`` of its lifecycle driver, and the same fields of
    each offering. A path is matched at the end of the request path, because a base URL may carry a prefix.
    """
    found: set[str] = {DEFAULT_HEALTH_PATH}
    scopes: list[Mapping[str, Any]] = [record]
    if isinstance(record.get("config"), Mapping):
        scopes.append(record["config"])
    for scope in list(scopes):
        offerings = scope.get("offerings")
        if isinstance(offerings, list):
            scopes.extend(item for item in offerings if isinstance(item, Mapping))
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


def publish(services: Mapping[str, Any] | None, *, extra_probe_paths: Callable[[Mapping[str, Any]], Iterable[str]] | None = None) -> None:
    """Replace the map from the services of the runtime config just published (one reference swap).

    ``extra_probe_paths`` names further metadata paths a record's kind reads (the loaded-state endpoint of its
    ``state_type``): the engine kinds are known to the caller, not to this module.
    """
    built: dict[str, list[str]] = {}
    paths: dict[str, set[str]] = {}
    for service_id, record in (services or {}).items():
        if not isinstance(record, Mapping):
            continue
        policy = policy_for(str(service_id), services)
        if not policy.enabled:
            continue
        declared = declared_probe_paths(record)
        if extra_probe_paths is not None:
            declared.update(path for path in map(_path_of, extra_probe_paths(record)) if path)
        for key in destinations_of(record):
            if policy.domain not in built.setdefault(key, []):
                built[key].append(policy.domain)
            paths.setdefault(key, set()).update(declared)
    global _destinations, _probe_paths
    _probe_paths = MappingProxyType({key: frozenset(value) for key, value in paths.items()})
    _destinations = MappingProxyType({key: tuple(domains) for key, domains in built.items()})


def _declared_probe(key: str, path: str) -> bool:
    return any(path == declared or path.endswith(declared) for declared in _probe_paths.get(key, ()))


def mapped_destinations() -> Mapping[str, tuple[str, ...]]:
    return _destinations


def check_request(request: httpx.Request) -> None:
    """Refuse a request to a transition-enabled domain's address unless the task holds a lease for it."""
    marked = request.extensions.pop(PROBE_EXTENSION, False) is True and request.method == "GET"
    key = destination_key(request.url.host, request.url.port, request.url.scheme)
    domains = _destinations.get(key)
    if not domains or (marked and _declared_probe(key, request.url.path)):
        return
    if any(holds_lease(domain) for domain in domains):
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

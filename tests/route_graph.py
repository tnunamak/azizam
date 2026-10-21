"""The route graph of the gateway app: every route, its handler, and its policy.

``capture`` reads what a request meets: each route as registered (path, methods,
handler, schema flags), the dispatch outcome of every method on every probed
path, the policy the protocol frontends project, and a digest of the OpenAPI
document. tests/test_route_graph_freeze.py compares a capture with
tests/fixtures/route_graph_frozen.json, which was written from origin/main before
the frontends mounted from registry/protocol_frontends.yml.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import route_matrix


def _endpoint(route: Any) -> str | None:
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return None
    return f"{getattr(endpoint, '__module__', '')}.{getattr(endpoint, '__qualname__', '')}"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _route_row(route: Any) -> dict[str, Any]:
    response_class = getattr(route, "response_class", None)
    extra = getattr(route, "openapi_extra", None)
    return {
        # The mount's wrapper (authentication, loss check) is an APIRoute that adds checks before the
        # frozen handler; the graph records it as the APIRoute it replaces.
        "type": "APIRoute" if type(route).__name__ in {"MountedRoute", "ManagementRoute"} else type(route).__name__,
        "path": getattr(route, "path", None),
        "methods": sorted(getattr(route, "methods", None) or []) or None,
        "name": getattr(route, "name", None),
        "endpoint": _endpoint(route),
        "include_in_schema": getattr(route, "include_in_schema", None),
        "response_class": getattr(response_class, "__name__", None),
        "status_code": getattr(route, "status_code", None),
        "openapi_extra": None if extra is None else _digest(extra),
    }


def routes(app) -> list[dict[str, Any]]:
    """Registration order is not part of the contract; dispatch is, and it is captured below."""
    return sorted((_route_row(route) for route in app.router.routes), key=lambda row: json.dumps(row, sort_keys=True))


def policy(proxy_module) -> dict[str, Any]:
    return {
        "runtime": proxy_module._RUNTIME_ROUTE_POLICY,
        "azizam": {path: list(spec) for path, spec in sorted(proxy_module._AZIZAM_POLICY.items())},
        "asset_content": proxy_module._ASSET_CONTENT_ROUTE_POLICY_BY_KIND,
        "aliases": [
            {"methods": list(entry["methods"]), "canonical": entry["canonical"], "alias": entry["alias"]}
            for entry in proxy_module.CANONICAL_PROTOCOL_ROUTE_ALIASES
        ],
    }


def openapi_digest(app) -> str:
    return _digest(app.openapi())


def capture(proxy_module) -> dict[str, Any]:
    app = proxy_module.app
    return {
        "routes": routes(app),
        "dispatch": route_matrix.routing_matrix(app),
        "policy": policy(proxy_module),
        "openapi_sha256": openapi_digest(app),
    }

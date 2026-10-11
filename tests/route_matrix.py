"""Route and method matrix used by the route baseline test.

The matrix has two parts:

* ``routing``: for every registered route path (parameters filled with sample
  values) and for the bare path and one unknown sub-path of every registered
  first segment, which route Starlette dispatches each method to.  A full
  match records the route; a method mismatch records 405 and the ``Allow``
  set of the first partially matching route.  Nothing executes.
* ``live``: for the paths whose outcome is not a real handler (405 or the
  catch-all), the status, ``Allow`` set and content type through the whole
  middleware stack, for no credential, an app key and an admin key, with an
  HTML and a ``*/*`` ``Accept``.  These requests never reach a handler that
  can change state.

Regenerate the fixture with ``tests/fixtures/route_method_baseline.json``
deleted outside the sandbox only when a reviewed route change requires it.
"""
from __future__ import annotations

import re

from starlette.routing import Match, Mount

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
ACCEPTS = {"html": "text/html,application/xhtml+xml", "any": "*/*"}
UNKNOWN = "zz-unknown-probe"


def _sample(path: str) -> str:
    path = re.sub(r"\{[^}:]+:path\}", "sample/x", path)
    path = re.sub(r"\{[^}:]+:int\}", "1", path)
    return re.sub(r"\{[^}]+\}", "sample", path)


def _flatten(routes, prefix=""):
    for route in routes:
        if isinstance(route, Mount) and route.routes:
            yield from _flatten(route.routes, prefix + route.path)
        else:
            yield prefix, route


def route_label(route) -> str:
    if isinstance(route, Mount):
        return f"mount {route.path}"
    endpoint = getattr(route, "endpoint", None)
    name = getattr(endpoint, "__name__", None) or getattr(route, "name", None) or type(route).__name__
    return f"{getattr(route, 'path', '')} {name}"


def _first_segment(path: str) -> str:
    return path.lstrip("/").split("/", 1)[0]


def probe_paths(app) -> list[str]:
    paths = {"/"}
    segments = set()
    for prefix, route in _flatten(app.router.routes):
        path = prefix + getattr(route, "path", "")
        if path in {"", "/{path:path}"}:
            continue
        paths.add(_sample(path))
        segment = _first_segment(path)
        if segment and "{" not in segment:
            segments.add(segment)
    for segment in segments:
        paths.update({f"/{segment}", f"/{segment}/", f"/{segment}/{UNKNOWN}"})
    paths.add(f"/{UNKNOWN}")
    return sorted(paths)


def dispatch(app, method: str, path: str) -> str:
    """Mirror Starlette's Router: first full match wins, else first partial."""
    scope = {"type": "http", "method": method, "path": path, "root_path": "",
             "headers": [], "query_string": b""}
    partial = None
    for route in app.router.routes:
        match, _child = route.matches(scope)
        if match == Match.FULL:
            return route_label(route)
        if match == Match.PARTIAL and partial is None:
            partial = route
    if partial is not None:
        return f"405 allow={','.join(sorted(partial.methods))} via {route_label(partial)}"
    return "unmatched"


def routing_matrix(app) -> dict:
    return {path: {method: dispatch(app, method, path) for method in METHODS}
            for path in probe_paths(app)}


def is_catch_all(outcome: str) -> bool:
    return outcome.startswith("/{path:path} ")


def live_targets(routing: dict) -> list[tuple[str, str]]:
    """Requests whose outcome no state-changing handler decides."""
    targets = []
    for path, by_method in routing.items():
        for method, outcome in by_method.items():
            if outcome.startswith("405 ") or is_catch_all(outcome):
                targets.append((method, path))
    return targets


def _outcome(response) -> str:
    allow = response.headers.get("allow")
    parts = [str(response.status_code), response.headers.get("content-type", "").split(";")[0] or "-"]
    if allow:
        parts.append("allow=" + ",".join(sorted(m.strip() for m in allow.split(","))))
    if "location" in response.headers:
        parts.append("location=" + response.headers["location"])
    return " ".join(parts)


def live_matrix(client, targets, principals: dict[str, dict]) -> dict:
    rows = {}
    for method, path in targets:
        cells = []
        for principal, headers in principals.items():
            seen = {}
            for accept_name, accept in ACCEPTS.items():
                client.cookies.clear()
                response = client.request(method, path, headers={**headers, "Accept": accept},
                                          follow_redirects=False)
                seen[accept_name] = _outcome(response)
            if seen["html"] == seen["any"]:
                cells.append(f"{principal}: {seen['any']}")
            else:
                cells.append(f"{principal}: html {seen['html']} | any {seen['any']}")
        rows[f"{method} {path}"] = "; ".join(cells)
    return rows

def baseline(app, client, admin_key: str, app_key: str) -> dict:
    routing = routing_matrix(app)
    principals = {"none": {}, "app": {"Authorization": f"Bearer {app_key}"},
                  "admin": {"Authorization": f"Bearer {admin_key}"}}
    return {"routing": routing, "live": live_matrix(client, live_targets(routing), principals)}


def mint_app_key(case) -> str:
    app = case.client.post("/admin/api/apps", headers=case._auth(), json={"name": "route-matrix"})
    assert app.status_code == 201, app.text
    minted = case.client.post(f'/admin/api/apps/{app.json()["app"]["id"]}/keys',
                              headers=case._auth(), json={"label": "route-matrix"})
    assert minted.status_code == 201, minted.text
    return minted.json()["credential"]["api_key"]

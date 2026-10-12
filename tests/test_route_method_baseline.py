"""Every route, method, status and Allow header matches the r18 baseline.

The baseline was recorded on release 18 (8a3147f0) before the web app moved
from /ui to the root. The current tree is probed over the union of the
baseline's paths and its own. Only these differences are allowed:

* paths that now classify as a web app shape (gateway/ui_routes.classify)
  are served by the UI route for every method;
* paths deleted with the move (the /ui mount, the server portal, the admin
  dashboard and its aliases, /firehose, /r/{id}, old login/logout aliases)
  now fall to the unchanged catch-all, or to the 405 of a route that remains;
* GET /setup/console is the server setup console's new path;
* GET and HEAD /showcase/ serve the static product page, and GET and HEAD
  /showcase answer 308 to it (r18: GET /showcase served the page, GET
  /showcase/ redirected to it, HEAD was 405). Other methods keep their answers;
* /showcase/docs and /showcase/docs/{path} are new: GET and HEAD serve the
  static product docs; every other method falls to the catch-all and answers
  exactly as it does for an unknown /showcase/ path;
* GET and HEAD with an HTML Accept on a first segment no route registers get
  the web app's 404 page (503 here: the sandbox has no web build). The same
  requests without HTML in Accept keep their r18 answer.

Everything else, including every API 405 and Allow header, must be equal.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import proxy
import route_matrix
import test_proxy_admin_integration as admin_integration
from gateway import accounts, ui_routes

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "route_method_baseline.json").read_text())
UI_ROUTE = route_matrix.route_label(proxy.WEB_UI_ROUTE)
DELETED_SEGMENTS = {"ui", "r", "firehose"}
ADMIN_SECTIONS = ("services", "status", "apps", "profiles", "users", "audit", "settings", "overview")
DELETED_PATHS = {
    "/admin", "/admin/", "/admin/login", "/admin/logout", "/me", "/me/", "/me/login", "/me/logout",
    "/logout", *(f"/admin/{name}{slash}" for name in ADMIN_SECTIONS for slash in ("", "/")),
}
NEW_PATHS = {"/setup/console"}
# The access plan API replaced these admin routes (docs/design/ACCESS_PLANS.md): profiles, runtime
# mappings, the policy simulator and editor, the LAN placeholder keys and the user-access defaults.
ACCESS_PLAN_API_PREFIXES = (
    "/admin/api/access-plans", "/admin/api/access/", "/admin/api/profiles", "/admin/api/runtime-profile-mappings",
    "/admin/api/policy/", "/admin/api/settings/lan-placeholder-keys", "/admin/api/operator/user-access-defaults",
)
SHOWCASE_PAGES = {"/showcase/": "/showcase/ showcase_page", "/showcase": "/showcase showcase_page_slashless"}
DOCS_ROUTES = {"/showcase/docs": "/showcase/docs docs_root", "/showcase/docs/sample/x": "/showcase/docs/{path:path} docs_page"}


def _flat(routing: dict) -> dict:
    return {f"{method} {path}": outcome for path, row in routing.items() for method, outcome in row.items()}


def _now_needs_a_key(old: str | None, new: str | None) -> bool:
    """A method nothing serves answers a caller with no key 401 (key_required), not 404."""
    if old is None or new is None:
        return False
    return re.sub(r"none: (404|405)[^;]*", "none: 401 application/json", old, count=1) == new


def _now_needs_a_key_any(old: str | None, new: str | None) -> bool:
    """Same as the API-client outcome, except a caller with no key now gets 401, not 404."""
    old_any, new_any = _any_accept(old), _any_accept(new)
    if old_any.get("none", "").startswith(("404", "405")) and new_any.get("none", "").startswith("401"):
        old_any["none"] = new_any["none"]
    return old_any == new_any


def _deliberate(key: str) -> str | None:
    """The reviewed class of a difference, or None if none may exist."""
    _method, path = key.split(" ", 1)
    if path.startswith(ACCESS_PLAN_API_PREFIXES) or (path.startswith("/admin/api/providers/") and path.endswith("/allowed-plans")):
        return "access-plans"
    if ui_routes.classify(path) is not None:
        return "ui"
    if path in DELETED_PATHS or ui_routes.first_segment(path) in DELETED_SEGMENTS:
        return "deleted"
    if path in NEW_PATHS:
        return "new"
    if path in SHOWCASE_PAGES and _method in {"GET", "HEAD"}:
        return "showcase"
    if path in DOCS_ROUTES:
        return "docs"
    if _method in {"GET", "HEAD"} and ui_routes.first_segment(path) not in proxy.WEB_UI_ROUTE.registered_segments:
        return "html404"
    return None


def _method_of(key: str) -> str:
    return key.split(" ", 1)[0]


def _any_accept(cells: str | None) -> dict:
    """Per principal, the outcome for Accept */* (an API client)."""
    result = {}
    for cell in (cells or "").split("; "):
        principal, _, outcome = cell.partition(": ")
        result[principal] = outcome.split(" | any ", 1)[1] if outcome.startswith("html ") else outcome
    return result


@pytest.fixture(scope="module")
def current():
    case = admin_integration.ProxyAdminIntegrationTests(methodName="runTest")
    case.setUp()
    try:
        accounts.initialize(case.db)
        yield route_matrix.baseline(proxy.app, case.client, case.admin_key, route_matrix.mint_app_key(case),
                                    extra_paths=BASELINE["routing"])
    finally:
        case.tearDown()


def test_routing_differs_from_baseline_only_where_reviewed(current):
    old, new = _flat(BASELINE["routing"]), _flat(current["routing"])
    unexpected = {}
    for key in sorted(set(old) | set(new)):
        if old.get(key) == new.get(key):
            continue
        kind = _deliberate(key)
        outcome = new.get(key, "")
        if kind == "ui" and outcome == UI_ROUTE:
            continue
        if kind == "deleted" and (route_matrix.is_catch_all(outcome) or outcome.startswith("405 ")):
            continue
        if kind == "access-plans":
            continue  # the access plan API replaced /admin/api/profiles
        if _now_needs_a_key(old.get(key), new.get(key)):
            continue
        if kind == "new" and key in {"GET /setup/console", "HEAD /setup/console"} | {
                f"{m} /setup/console" for m in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS")}:
            continue
        if kind == "showcase" and outcome == SHOWCASE_PAGES[key.split(" ", 1)[1]]:
            continue
        if kind == "docs" and key not in old:
            route = DOCS_ROUTES[key.split(" ", 1)[1]]
            if outcome == route if _method_of(key) in {"GET", "HEAD"} else route_matrix.is_catch_all(outcome):
                continue
        unexpected[key] = (old.get(key), outcome)
    assert unexpected == {}


def test_live_outcomes_differ_from_baseline_only_where_reviewed(current):
    old, new = BASELINE["live"], current["live"]
    unexpected = {}
    for key in sorted(set(old) | set(new)):
        if old.get(key) == new.get(key):
            continue
        kind = _deliberate(key)
        if kind in {"ui", "deleted", "new", "access-plans"}:
            continue
        if _now_needs_a_key(old.get(key), new.get(key)):
            continue
        if kind == "html404" and _now_needs_a_key_any(old.get(key), new.get(key)):
            continue
        if kind == "showcase" and key not in new:
            continue
        unknown = f"{_method_of(key)} /showcase/{route_matrix.UNKNOWN}"
        if kind == "docs" and key not in old and new.get(key) == new.get(unknown) is not None:
            continue
        unexpected[key] = (old.get(key), new.get(key))
    assert unexpected == {}


def test_deliberate_head_changes():
    routing = route_matrix.routing_matrix(proxy.app, ["/", "/admin"])
    assert routing["/"]["HEAD"] == UI_ROUTE
    assert routing["/admin"]["HEAD"].startswith("405 allow=DELETE,GET,OPTIONS,PATCH,POST,PUT via /{path:path}")


def test_api_protocol_paths_keep_their_r18_answers(current):
    for key in ("HEAD /v1/models", "HEAD /mcp", "HEAD /health", "HEAD /llms.txt", "GET /mcp", "DELETE /mcp"):
        if key in BASELINE["live"]:
            assert current["live"][key] == BASELINE["live"][key], key
    assert _flat(current["routing"])["HEAD /api/blobs/sample"] == _flat(BASELINE["routing"])["HEAD /api/blobs/sample"]

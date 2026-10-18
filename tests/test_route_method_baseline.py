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
* /v1/assets/{asset_id}/thumbnail is new (signed WebP preview). Every method
  must answer exactly as the sibling /v1/assets/{asset_id}/content route does:
  GET is the one route, every other method is the same 405 or catch-all;
* the MCP authorization server's paths are new (/.well-known/oauth-authorization-server,
  /oauth/..., /me/api/mcp/...): each answers with its own route or that route's
  405; in r18 they fell to the catch-all;
* Add engine replaced the engine preset list: /admin/api/engine-definitions and
  /admin/api/engines/... are new admin routes (tests/test_engine_admin_routes.py
  covers them), and /admin/api/operator/service-presets is deleted and falls to
  the catch-all or a 405;
* /admin/api/operator/host-commands is the new owner-only command permission
  (tests/test_admin_owner_holes_round4.py covers its write boundary);
* GET and PUT /admin/api/settings/sessions (the owner's session settings), POST /auth/api/reauth and
  POST /auth/api/sign-out-everywhere are new (tests/test_session_lifecycle.py and
  tests/test_session_settings_api.py cover them); every other method falls to the catch-all as before;
* POST /me/api/media/remove-missing is new (Library "Remove missing items"): that
  method is its own route, and every other method answers as /me/api/media/{asset_id}
  did before (the sample path /me/api/media/sample);
* POST /me/api/media/confirm-folder is new (Library "This is the right media folder"):
  the owner's marker confirmation is its own route, and every other method answers as
  /me/api/media/{asset_id} did before, as for remove-missing;
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
# Per-engine native pass-through (contract section 5): new since the baseline, off unless an engine enables it.
NATIVE_PREFIX = "/native"
# The access plan API replaced these admin routes (docs/design/ACCESS_PLANS.md): profiles, runtime
# mappings, the policy simulator and editor, the LAN placeholder keys and the user-access defaults.
ACCESS_PLAN_API_PREFIXES = (
    "/admin/api/access-plans", "/admin/api/access/", "/admin/api/profiles", "/admin/api/runtime-profile-mappings",
    "/admin/api/policy/", "/admin/api/settings/lan-placeholder-keys", "/admin/api/operator/user-access-defaults",
)
MCP_OAUTH_PREFIXES = ("/.well-known/oauth-authorization-server", "/oauth", "/me/api/mcp/")
ENGINE_PATHS = ("/admin/api/engine-definitions", "/admin/api/engines/")
DELETED_PRESETS = "/admin/api/operator/service-presets"
HOST_COMMANDS = "/admin/api/operator/host-commands"
SESSION_PATHS = {"/admin/api/settings/sessions", "/auth/api/reauth", "/auth/api/sign-out-everywhere"}
THUMBNAIL_PATH, THUMBNAIL_SIBLING = "/v1/assets/sample/thumbnail", "/v1/assets/sample/content"
REMOVE_MISSING_PATH, REMOVE_MISSING_SIBLING = "/me/api/media/remove-missing", "/me/api/media/sample"
REMOVE_MISSING_ROUTE = "/me/api/media/remove-missing remove_missing_media"
CONFIRM_FOLDER_PATH = "/me/api/media/confirm-folder"
CONFIRM_FOLDER_ROUTE = "/me/api/media/confirm-folder confirm_media_folder"


SHARDS = 4


def _group(path: str) -> str:
    """Rows that one comparison reads together (a thumbnail with the content route) form one group, and a group
    is never split between shards."""
    return THUMBNAIL_SIBLING if path == THUMBNAIL_PATH else path


def _shard_map(current: dict) -> dict[str, int]:
    """Every path of the baseline and of the current tree to the live-outcome item that owns it: the sorted
    groups are dealt out in turn, so each item gets the same mix of cheap and costly paths."""
    paths = {key.split(" ", 1)[1] for key in BASELINE["live"]} | {path for _, path in route_matrix.live_targets(current["routing"])}
    dealt = {group: index % SHARDS for index, group in enumerate(sorted({_group(path) for path in paths}))}
    return {path: dealt[_group(path)] for path in paths}


def _flat(routing: dict) -> dict:
    return {f"{method} {path}": outcome for path, row in routing.items() for method, outcome in row.items()}


def _now_needs_a_key(old: str | None, new: str | None) -> bool:
    """A method nothing serves answers a caller with no key 401 (key_required), not 404."""
    if old is None or new is None:
        return False
    return re.sub(r"none: (404|405)[^;]*", "none: 401 application/json", old, count=1) == new


def _same_as_sibling_or_needs_a_key(sibling: str | None, new: str | None) -> bool:
    return sibling is not None and (sibling == new or _now_needs_a_key(sibling, new))


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
    if path.startswith("/admin/api/apps/") and "/trusted-addresses" in path:
        return "trusted-addresses"  # owner routes added after the baseline (gateway/trusted_addresses.py)
    if path == "/admin/api/signin" or path.startswith("/admin/api/signin/"):
        return "signin"  # owner routes added after the baseline (gateway/signin_api.py)
    if path.startswith(ENGINE_PATHS) or path == DELETED_PRESETS:
        return "engines"  # Add engine (gateway/engine_admin.py) replaced the preset list
    if path == HOST_COMMANDS:
        return "host_commands"
    if path in SESSION_PATHS:
        return "sessions"
    if ui_routes.classify(path) is not None:
        return "ui"
    if path in DELETED_PATHS or ui_routes.first_segment(path) in DELETED_SEGMENTS:
        return "deleted"
    if path == NATIVE_PREFIX or path.startswith(NATIVE_PREFIX + "/"):
        return "native"
    if path in NEW_PATHS:
        return "new"
    if path == THUMBNAIL_PATH:
        return "thumbnail"
    if path in (REMOVE_MISSING_PATH, CONFIRM_FOLDER_PATH):
        return "media_folder"
    if path.startswith(MCP_OAUTH_PREFIXES):
        return "mcp_oauth"
    if _method in {"GET", "HEAD"} and ui_routes.first_segment(path) not in proxy.WEB_UI_ROUTE.registered_segments:
        return "html404"
    return None


def _media_folder_path(key: str) -> str:
    return CONFIRM_FOLDER_PATH if CONFIRM_FOLDER_PATH in key else REMOVE_MISSING_PATH


def _media_folder_sibling(key: str) -> str:
    return key.replace(_media_folder_path(key), REMOVE_MISSING_SIBLING)


def _thumbnail_sibling(key: str) -> str:
    return key.replace(THUMBNAIL_PATH, THUMBNAIL_SIBLING)


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
    """Routing is pure inspection: no request is made to build it."""
    return {"routing": route_matrix.routing_matrix(proxy.app, extra_paths=BASELINE["routing"])}


@pytest.fixture
def live_client():
    """A client and the three principals; every live-outcome item gets its own admin store."""
    case = admin_integration.ProxyAdminIntegrationTests(methodName="runTest")
    case.setUp()
    try:
        accounts.initialize(case.db)
        app_key = route_matrix.mint_app_key(case)
        with route_matrix.one_portal(case.client):
            yield case.client, {"none": {}, "app": {"Authorization": f"Bearer {app_key}"},
                                "admin": {"Authorization": f"Bearer {case.admin_key}"}}
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
        if kind == "trusted-addresses":
            continue  # new owner routes; tests/test_trusted_addresses.py covers them
        if kind == "signin":
            continue  # new owner routes; tests/test_signin_settings_api.py and test_signin_test_flow.py cover them
        if kind == "native" and key not in old:
            continue  # a new lane; tests/test_native_passthrough.py covers it
        if kind == "engines" and (key not in old or route_matrix.is_catch_all(outcome) or outcome.startswith("405 ")):
            continue  # new Add engine routes, or the deleted preset list falling through
        if kind == "host_commands" and key not in old:
            continue  # new owner command permission route
        if kind == "sessions" and key not in old:
            continue  # new session settings, step-up and sign-out-everywhere routes
        if _now_needs_a_key(old.get(key), new.get(key)):
            continue
        if kind == "new" and key in {"GET /setup/console", "HEAD /setup/console"} | {
                f"{m} /setup/console" for m in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS")}:
            continue
        if kind == "media_folder" and key not in old:
            # POST is the new route itself and must say so; every other method answers as the sibling does.
            expected = (CONFIRM_FOLDER_ROUTE if CONFIRM_FOLDER_PATH in key else REMOVE_MISSING_ROUTE) if _method_of(key) == "POST" else new.get(_media_folder_sibling(key))
            if outcome == expected:
                continue
        if kind == "thumbnail" and key not in old:
            same_as_content = new.get(_thumbnail_sibling(key), "").replace("/content", "/thumbnail").replace(
                "assets_content", "assets_thumbnail")
            if outcome == same_as_content:
                continue
        if kind == "mcp_oauth" and (key not in old or route_matrix.is_catch_all(old[key])) and (
                not route_matrix.is_catch_all(outcome) or key not in old):
            continue
        unexpected[key] = (old.get(key), outcome)
    assert unexpected == {}


@pytest.mark.parametrize("shard", range(SHARDS))
@pytest.mark.parametrize("method", route_matrix.METHODS)
def test_live_outcomes_differ_from_baseline_only_where_reviewed(current, live_client, method, shard):
    # One item per method and path shard so the sweep spreads over workers; together they cover every baseline row.
    assert {_method_of(key) for key in BASELINE["live"]} <= set(route_matrix.METHODS)
    client, principals = live_client
    owner = _shard_map(current)
    targets = [(verb, path) for verb, path in route_matrix.live_targets(current["routing"])
               if verb == method and owner[path] == shard]
    old = {key: cells for key, cells in BASELINE["live"].items()
           if _method_of(key) == method and owner[key.split(" ", 1)[1]] == shard}
    new = route_matrix.live_matrix(client, targets, principals)
    unexpected = {}
    for key in sorted(set(old) | set(new)):
        if old.get(key) == new.get(key):
            continue
        kind = _deliberate(key)
        if kind in {"ui", "deleted", "new", "access-plans", "mcp_oauth", "trusted-addresses", "signin", "engines", "host_commands", "sessions", "native"}:
            continue
        if _now_needs_a_key(old.get(key), new.get(key)):
            continue
        if kind == "media_folder" and key not in old:
            # A POST has a real handler, so it is never a live row; the others answer as the sibling did.
            assert _method_of(key) != "POST", key
            if _same_as_sibling_or_needs_a_key(BASELINE["live"].get(_media_folder_sibling(key)), new.get(key)):
                continue
        if kind == "html404" and _now_needs_a_key_any(old.get(key), new.get(key)):
            continue
        if kind == "thumbnail" and key not in old and new.get(key) == new.get(_thumbnail_sibling(key)) is not None:
            continue
        unexpected[key] = (old.get(key), new.get(key))
    assert unexpected == {}


def test_every_baseline_and_current_live_row_belongs_to_exactly_one_shard(current):
    owner = _shard_map(current)
    keys = set(BASELINE["live"]) | {f"{verb} {path}" for verb, path in route_matrix.live_targets(current["routing"])}
    owned = [sorted(key for key in keys if owner[key.split(" ", 1)[1]] == shard) for shard in range(SHARDS)]
    assert sorted(key for shard in owned for key in shard) == sorted(keys)
    assert all(owned)
    # A pair that one comparison reads together is never split.
    assert owner[THUMBNAIL_PATH] == owner[THUMBNAIL_SIBLING]


def test_deliberate_head_changes():
    routing = route_matrix.routing_matrix(proxy.app, ["/", "/admin"])
    assert routing["/"]["HEAD"] == UI_ROUTE
    assert routing["/admin"]["HEAD"].startswith("405 allow=DELETE,GET,OPTIONS,PATCH,POST,PUT via /{path:path}")


def test_api_protocol_paths_keep_their_r18_answers(current, live_client):
    keys = ("HEAD /v1/models", "HEAD /mcp", "HEAD /health", "HEAD /llms.txt", "GET /mcp", "DELETE /mcp")
    required = {key for key in keys if key in BASELINE["live"]}
    targets = [(method, path) for method, path in route_matrix.live_targets(current["routing"])
               if f"{method} {path}" in required]
    # A sentinel that is no longer a 405 or catch-all target must fail here, as it did when the whole matrix ran.
    assert {f"{method} {path}" for method, path in targets} == required
    client, principals = live_client
    live = route_matrix.live_matrix(client, targets, principals)
    for key in sorted(required):
        assert live[key] == BASELINE["live"][key], key
    assert _flat(current["routing"])["HEAD /api/blobs/sample"] == _flat(BASELINE["routing"])["HEAD /api/blobs/sample"]


def test_remove_missing_is_a_session_route_that_other_callers_cannot_use(current, live_client):
    assert _flat(current["routing"])[f"POST {REMOVE_MISSING_PATH}"] == REMOVE_MISSING_ROUTE
    client, principals = live_client
    answers = {name: client.post(REMOVE_MISSING_PATH, headers=headers).status_code
               for name, headers in principals.items()}
    assert answers["none"] == 401
    assert all(400 <= status < 500 for status in answers.values()), answers

"""Registered-route coverage, schema stability, and real credential boundaries."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import Mount, Route, WebSocketRoute

import proxy
from gateway import accounts, admin_store, metrics_writer, providers, receipt_store
from gateway.admin_api import _require_admin
from gateway.admin_oidc import CookieConfig, csrf_token_for_session, encode_session_cookie, session_from_claims
from gateway.owner_openapi import OWNER_OPERATIONS, augment_owner_openapi, walk_routes
from gateway.ui_routes import UiRoute
from gateway.ui_static import UIStaticFiles
import test_proxy_admin_integration as admin_integration

FIXTURES = Path(__file__).parent / "fixtures"
INVENTORY = json.loads((FIXTURES / "headless_routes.json").read_text())
OWNER_ROUTES = [row for row in INVENTORY if row["auth"] == "owner-bearer"]


def _inventoried(path):
    return path.startswith(("/admin", "/me", "/auth", "/setup")) or path in {
        "/status", "/status/", "/status/raw", "/status/raw/", "/logout", "/login"}


def _registered_routes(routes):
    rows = []
    for path, route in walk_routes(routes):
        if type(route) is UiRoute:
            # The web export: registry pages, payloads and assets only (test_ui_routes).
            continue
        if isinstance(route, Mount):
            assert route.routes, f"Unclassified opaque mount: {path}"
            continue
        assert isinstance(route, (Route, WebSocketRoute)), f"Unclassified route type: {path}"
        methods = {"WEBSOCKET"} if isinstance(route, WebSocketRoute) else route.methods
        assert methods, f"Unclassified methodless route: {path}"
        for method in sorted(methods):
            rows.append((method, path, route.endpoint.__module__ + "." + route.name))
    assert len(rows) == len({(method, path) for method, path, _handler in rows}), "Duplicate method/path registrations"
    return rows


def test_inventory_covers_registered_routes_and_documentation():
    registered = _registered_routes(proxy.app.routes)
    actual = {row for row in registered if _inventoried(row[1])}
    assert actual == {(r["method"], r["path"], r["handler"]) for r in INVENTORY}
    outside = sorted(list(row) for row in registered if not _inventoried(row[1]))
    assert outside == json.loads((FIXTURES / "headless_non_control_routes.json").read_text()), json.dumps(outside)
    assert all(row["auth"] in {"owner-bearer", "session-only", "session-view", "browser/public"}
               for row in INVENTORY)
    documented = set(re.findall(r"\| `(GET|POST|PUT|PATCH|DELETE|HEAD) ([^`]+)` \|", (
        Path(__file__).parents[1] / "docs/ops/HEADLESS_ACCESS.md").read_text()))
    assert documented == {(r["method"], r["path"]) for r in INVENTORY}
    assert set(OWNER_OPERATIONS) == {
        f'{r["method"]} {r["path"]}' for r in OWNER_ROUTES if r["path"].startswith("/admin/api/")}


@pytest.mark.parametrize("kind", ["mounted", "http", "websocket", "opaque"])
def test_inventory_rejects_unclassified_route_types(kind):
    async def reset(request):
        return JSONResponse({"ok": True})

    if kind == "mounted":
        extension = FastAPI()
        extension.add_api_route("/reset", reset, methods=["POST"], include_in_schema=False)
        route = Mount("/admin/api/extension", app=extension)
    elif kind == "http":
        route = Route("/admin/api/reset", reset, methods=["POST"])
    elif kind == "websocket":
        route = WebSocketRoute("/admin/api/reset", reset)
    else:
        route = Mount("/admin/api/extension", app=reset)
    with patch.object(proxy.app.router, "routes", [*proxy.app.routes, route]):
        with pytest.raises(AssertionError):
            test_inventory_covers_registered_routes_and_documentation()


def test_walk_preserves_nested_mount_prefixes_and_http_and_websocket_methods():
    async def endpoint(request):
        return JSONResponse({"ok": True})

    routes = [Mount("/admin", routes=[Mount("/api", routes=[
        Route("/extension", endpoint, methods=["GET"]),
        WebSocketRoute("/events", endpoint),
    ])])]
    assert _registered_routes(routes) == [
        ("GET", "/admin/api/extension", __name__ + ".endpoint"),
        ("HEAD", "/admin/api/extension", __name__ + ".endpoint"),
        ("WEBSOCKET", "/admin/api/events", __name__ + ".endpoint"),
    ]


def test_only_the_ui_route_type_is_exempt(tmp_path):
    async def guard(request, name):
        return None

    assert _registered_routes([UiRoute(tmp_path, guard)]) == []
    with pytest.raises(AssertionError, match="opaque mount"):
        _registered_routes([Mount("/ui", app=UIStaticFiles(directory=tmp_path), name="web_ui")])


def test_owner_schema_describes_nested_mounts_and_rejects_unsupported_http_routes():
    async def endpoint(request: Request):
        return JSONResponse({"ok": True})

    key = "POST /admin/api/extension/reset"
    metadata = {"tag": "Extension", "gate": "Owner role required"}
    with patch.dict(OWNER_OPERATIONS, {key: metadata}):
        leaf = APIRoute("/reset", endpoint, methods=["POST"], include_in_schema=False)
        schema = {"paths": {}}
        augment_owner_openapi(schema, [Mount("/admin", routes=[Mount("/api/extension", routes=[leaf])])])
        assert schema["paths"][key.split(" ", 1)[1]]["post"]["security"] == [{"OwnerBearer": []}]
        assert leaf.path == "/reset" and leaf.include_in_schema is False
        with pytest.raises(ValueError, match="requires an APIRoute"):
            augment_owner_openapi({"paths": {}}, [Route(key.split(" ", 1)[1], endpoint, methods=["POST"])])


def test_guide_reads_credentials_into_private_files(tmp_path, monkeypatch):
    guide = (Path(__file__).parents[1] / "docs/ops/HEADLESS_ACCESS.md").read_text()
    assert "AZIZAM_OWNER_KEY" not in guide
    assert '-H "Authorization: Bearer' not in guide
    assert "-H @owner-header.txt" in guide
    assert guide.count("set +x") >= 2
    scripts = re.findall(r"\./venv/bin/python - <<'PY'\n(.*?)\nPY", guide, re.DOTALL)
    assert len(scripts) == 2
    monkeypatch.chdir(tmp_path)
    key = "fixture-owner-secret"
    (tmp_path / "owner-key.txt").write_text(key + "\n")
    config = tmp_path / "restish-owner.json"
    auth = {"type": "bearer", "params": {"token": "configure-from-private-file"}}
    config.write_text(json.dumps({"apis": {"azizam": {"profiles": {"default": {
        "auth": copy.deepcopy(auth), "credentials": {"OwnerBearer": {"auth": copy.deepcopy(auth)}}}}}}}))
    config.chmod(0o600)
    monkeypatch.setenv("RSH_CONFIG", str(config))
    for script in scripts:
        exec(compile(script, "HEADLESS_ACCESS.md", "exec"), {})
    header = tmp_path / "owner-header.txt"
    assert header.read_text() == "Authorization: Bearer " + key + "\n"
    assert header.stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o600
    profile = json.loads(config.read_text())["apis"]["azizam"]["profiles"]["default"]
    assert profile["auth"]["params"]["token"] == key
    assert profile["credentials"]["OwnerBearer"]["auth"]["params"]["token"] == key


def test_owner_probe_detects_a_post_auth_session_gate(principals, gateway_test_state_dir):
    async def session_regression(request: Request):
        context = _require_admin(proxy.ADMIN_AUTH, request)
        if isinstance(context, JSONResponse):
            return context
        if context.auth_method != "session":
            return JSONResponse({"error": {"code": "session_required"}}, status_code=403)
        return JSONResponse({"ok": True})

    route = APIRoute("/admin/api/session-regression", session_regression, methods=["GET"])
    row = {"method": "GET", "path": route.path}
    with patch.object(proxy.app.router, "routes", [route, *proxy.app.routes]):
        with pytest.raises(AssertionError):
            test_every_owner_route_reaches_handler_with_owner_bearer(row, principals, gateway_test_state_dir)


def test_member_probe_detects_role_bypass_behind_action_guard(principals, gateway_test_state_dir):
    async def role_regression(request: Request):
        if not request.cookies.get(proxy.ADMIN_AUTH.cookie.name):
            return _require_admin(proxy.ADMIN_AUTH, request)
        session = proxy._require_user_session(request)
        denied = proxy._require_user_action_header(request, session)
        return denied if denied is not None else JSONResponse({"ok": True})

    route = APIRoute("/admin/api/role-regression", role_regression, methods=["POST"])
    row = {"method": "POST", "path": route.path}
    with patch.object(proxy.app.router, "routes", [route, *proxy.app.routes]):
        with pytest.raises(AssertionError):
            test_every_owner_route_rejects_app_and_member_without_effects(row, principals, gateway_test_state_dir)


def test_openapi_snapshot_and_security():
    proxy.app.openapi_schema = None
    schema = proxy.app.openapi()
    assert schema == json.loads((FIXTURES / "headless_openapi.json").read_text())
    ids = []
    for key, metadata in OWNER_OPERATIONS.items():
        method, path = key.split(" ", 1)
        operation = schema["paths"][path][method.lower()]
        assert operation["security"] == [{"OwnerBearer": []}]
        assert operation["tags"] == [metadata["tag"]]
        path_names = set(re.findall(r"\{([^}]+)\}", path))
        assert path_names == {parameter["name"] for parameter in operation["parameters"]
                              if parameter["in"] == "path"}
        ids.append(operation["operationId"])
    assert len(ids) == len(set(ids))
    for row in INVENTORY:
        if row["auth"] == "session-only":
            assert row["method"].lower() not in schema["paths"].get(row["path"], {})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def test_existing_data_plane_schema_is_byte_equivalent():
    """Digests were captured from the unmodified base commit, before the overlay."""
    schema = proxy.app.openapi()
    baseline = json.loads((FIXTURES / "headless_data_plane_hashes.json").read_text())
    for path, expected in baseline["paths"].items():
        operation = copy.deepcopy(schema["paths"][path])
        if path == "/gateway/v1/media/jobs/{job_id}/events":
            # The per-job cursor is an additive opt-in; the legacy query and
            # every other part of the original operation remain unchanged.
            operation["get"]["parameters"] = [p for p in operation["get"]["parameters"]
                                                 if p["name"] != "after_sequence"]
        assert _digest(operation) == expected, path
    for name, expected in baseline["schemas"].items():
        assert _digest(schema["components"]["schemas"][name]) == expected, name


@pytest.fixture(scope="module")
def principals():
    # Reuse the existing integration scaffolding and real SQLite admin-key mint.
    case = admin_integration.ProxyAdminIntegrationTests(methodName="runTest")
    case.setUp()
    try:
        accounts.initialize(case.db)
        providers.ensure_ledger(path=proxy.ADMIN_DB_PATH.parent / "provider_spend.db")
        receipt_store.init_db()
        app = case.client.post("/admin/api/apps", headers=case._auth(), json={"name": "auth-fixture"})
        assert app.status_code == 201
        minted = case.client.post(f'/admin/api/apps/{app.json()["app"]["id"]}/keys',
                                 headers=case._auth(), json={"label": "auth-fixture"})
        assert minted.status_code == 201
        case.app_key = minted.json()["credential"]["api_key"]
        cookie = CookieConfig(secure=False)
        proxy.ADMIN_AUTH.signing_key = b"s" * 32
        proxy.ADMIN_AUTH.cookie = cookie
        member = admin_store.upsert_oidc_user("https://idp.example", "headless-member",
                                            email="member@example.com", path=case.db)
        session = session_from_claims({"sub": member["id"], "email": member["email"]}, role="none", cookie=cookie)
        case.member_cookie = (cookie.name, encode_session_cookie(session, signing_key=b"s" * 32))
        case.member_headers = {
            "Accept": "application/json",
            "x-azizam-csrf": csrf_token_for_session(session, signing_key=b"s" * 32),
            "x-azizam-admin-action": "1",
            "x-azizam-user-action": "1",
        }
        # Initialize the category store before measuring request-side effects.
        case.client.get("/admin/api/categories/history", headers=case._auth())
        yield case
    finally:
        case.tearDown()


def _request(row):
    path = re.sub(r"\{(?:reservation_id|event_id)\}", "1", row["path"])
    path = re.sub(r"\{[^}]+\}", "missing-fixture-id", path)
    # Supply valid typed bodies so the request reaches auth instead of 422.
    body = {"profile_id": "default-local"} if path == "/admin/api/invites" else {}
    if path == "/admin/api/operator/sign-in":
        body = {"mode": "both"}
    return path, body


def _drain_state():
    """Drain state without clock-derived fields, which move between two reads."""
    snapshot = dict(proxy.DRAIN_STATE.snapshot())
    for key in [k for k in snapshot if k.startswith("seconds_since") or k.endswith("_age_s")]:
        snapshot.pop(key)
    return snapshot


def _settle_metrics_writer():
    """The metrics writer creates and commits metrics.db on its own thread,
    started by whichever earlier test in this worker first recorded a sample.
    Wait for that work so a snapshot never catches the file mid-creation;
    anything the request under test records is still flushed into the next
    snapshot and still fails the comparison."""
    writer = metrics_writer._writer
    if writer is not None and writer.stats["accepting"]:
        writer.wait_ready()
        writer.flush()


def _database_contents(root):
    _settle_metrics_writer()
    result = {}
    for path in sorted(root.rglob("*.db")):
        try:
            with sqlite3.connect(path) as connection:
                result[str(path)] = _digest(list(connection.iterdump()))
        except sqlite3.DatabaseError:
            # Other tests leave non-SQLite files named *.db; compare their bytes.
            result[str(path)] = _digest([path.read_bytes().hex()])
    return result


@pytest.mark.parametrize("row", OWNER_ROUTES, ids=lambda row: row["method"] + " " + row["path"])
def test_every_owner_route_rejects_app_and_member_without_effects(row, principals, gateway_test_state_dir):
    case = principals
    path, body = _request(row)
    before = _database_contents(gateway_test_state_dir)
    fixture_before = _database_contents(case.db.parent)
    drain_before = _drain_state()
    config_before = copy.deepcopy(proxy.config)
    for principal in ("app", "member"):
        case.client.cookies.clear()
        headers = {"Authorization": f"Bearer {case.app_key}"} if principal == "app" else case.member_headers
        if principal == "member":
            case.client.cookies.set(*case.member_cookie)
        response = case.client.request(row["method"], path, json=body, headers=headers, follow_redirects=False)
        if not path.startswith("/admin/api/") and response.status_code == 302:
            # HTML entry points deny by redirecting members to their own portal.
            assert principal == "member"
            assert response.headers["location"] == "/"
        else:
            assert response.status_code == 403, (row["method"], row["path"], principal, response.status_code)
            assert response.json()["error"]["code"] == "admin_forbidden"
        assert _database_contents(gateway_test_state_dir) == before
        assert _database_contents(case.db.parent) == fixture_before
        assert _drain_state() == drain_before
        assert proxy.config == config_before


@pytest.mark.parametrize("row", OWNER_ROUTES, ids=lambda row: row["method"] + " " + row["path"])
def test_every_owner_route_reaches_handler_with_owner_bearer(row, principals, gateway_test_state_dir):
    """Exercise the handler past authorization with no operational mutation."""
    path, body = _request(row)
    principals.client.cookies.clear()
    before = _database_contents(gateway_test_state_dir)
    fixture_before = _database_contents(principals.db.parent)
    drain_before = _drain_state()
    config_before = copy.deepcopy(proxy.config)
    # Config reload and global-cap deletion have no invalid-body/dry contract.
    # Stop at their first effect, after the route has completed authorization.
    effect = {
        ("POST", "/admin/api/config/reload"): (proxy, "reload_runtime_config"),
        ("DELETE", "/admin/api/providers/-/global-caps"): (proxy.ADMIN_STORE, "update_provider_admin_state"),
    }.get((row["method"], row["path"]))
    if effect:
        with patch.object(*effect, side_effect=OwnerOperationReached) as operation:
            try:
                principals.client.request(row["method"], path, json=body,
                                          headers=principals._auth(), follow_redirects=False)
            except OwnerOperationReached:
                pass
        operation.assert_called_once()
    else:
        # Typed bodies must pass FastAPI validation and fail inside the handler.
        typed = row.get("handler", "").startswith("gateway.accounts_api.")
        if typed and row["method"] not in {"GET", "DELETE"}:
            body = {**body, "expires_in": 0} if "/invites" in path or path.endswith("/reset") else body
            payload = {"json": body}
        elif row["method"] == "GET":
            payload = {}
        else:
            payload = {"content": "[]", "headers": {**principals._auth(), "Content-Type": "application/json"}}
        headers = payload.pop("headers", principals._auth())
        with patch.object(type(proxy._LITELLM_KEY_CLIENT), "list_keys", AsyncMock(return_value=[])):
            response = principals.client.request(row["method"], path, **payload,
                headers=headers, follow_redirects=False)
        assert response.status_code in {200, 302, 400, 404, 409, 412, 428, 503}, (
            row["method"], row["path"], response.status_code)
        if response.status_code >= 400:
            assert response.json()["error"]["code"] in HANDLER_ERRORS, (
                row["method"], row["path"], response.json()["error"]["code"])
        elif response.is_redirect:
            assert response.headers["location"].startswith("/") and not response.headers["location"].startswith("//")
    assert _database_contents(gateway_test_state_dir) == before
    assert _database_contents(principals.db.parent) == fixture_before
    assert _drain_state() == drain_before
    assert proxy.config == config_before


class OwnerOperationReached(Exception):
    """Test-only stop at a handler's first operational effect."""


HANDLER_ERRORS = {
    "invalid_json", "invalid_request", "invalid_expiry", "empty_update",
    "app_not_found", "profile_not_found", "credential_not_found", "provider_not_found",
    "account_not_found", "invite_not_found", "link_unavailable", "runtime_mapping_not_found", "extension_package_not_found",
    "revision_not_found", "no_active_snapshot", "invalid_retention",
    "settings_revision_required", "registry_revision_required", "litellm_unconfigured",
    "generation_mismatch", "audit_event_not_found", "person_not_found",
    "invalid_defaults", "invalid_instance", "invalid_lan_placeholder_keys", "invalid_prompt_memory",
}


@pytest.mark.parametrize("row", [row for row in INVENTORY if row["auth"] == "session-only"],
                         ids=lambda row: row["method"] + " " + row["path"])
def test_session_only_routes_do_not_accept_standalone_owner_bearer(row, principals):
    principals.client.cookies.clear()
    path, body = _request(row)
    response = principals.client.request(row["method"], path, json=body,
        headers=principals._auth(), follow_redirects=False)
    assert response.status_code in {401, 403, 302, 303}, (row["method"], row["path"], response.status_code)
    if response.is_redirect:
        assert response.headers["location"] in {"/setup/", "/sign-in/"}


@pytest.mark.parametrize("row", [row for row in INVENTORY if row["auth"] == "session-view"],
                         ids=lambda row: row["method"] + " " + row["path"])
def test_optional_session_views_do_not_treat_bearer_as_a_browser_session(row, principals):
    principals.client.cookies.clear()
    for key in (principals.admin_key, principals.app_key):
        response = principals.client.get(row["path"], headers={"Authorization": f"Bearer {key}"})
        assert response.status_code == 200
        assert response.json()["authenticated"] is False
        if row["path"] == "/me/api/activity":
            assert response.json()["receipts"] == []

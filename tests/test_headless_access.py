"""Registered-route coverage, schema stability, and real credential boundaries."""
from __future__ import annotations

import ast
import os
import copy
import functools
import hashlib
import json
import re
import sqlite3
import time
from html import escape
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.testclient import WebSocketDenialResponse

import proxy
from gateway import accounts, admin_store, frontend_mount, metrics_writer, operation_security, providers, receipt_store, runtime_auth, thumbnails
from tests.support.app_keys import issue_app_key
from gateway.admin_api import _require_admin
from gateway.admin_oidc import CookieConfig, csrf_token_for_session, encode_session_cookie, session_from_claims
from gateway.owner_openapi import (
    OWNER_AND_ADMINS_NOTE, OWNER_AND_ADMINS_TAG, OWNER_ONLY_NOTE, OWNER_ONLY_TAG, OWNER_OPERATIONS,
    augment_owner_openapi, walk_routes)
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
        assert schema["paths"][key.split(" ", 1)[1]]["post"]["security"] == operation_security.security_for(operation_security.OWNER)
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


def test_headless_snapshots_have_stable_key_order():
    for name in ("headless_openapi.json", "headless_routes.json"):
        snapshot = (FIXTURES / name).read_text()
        assert snapshot == json.dumps(json.loads(snapshot), indent=2, sort_keys=True) + "\n"


def test_openapi_snapshot_and_security():
    proxy.app.openapi_schema = None
    schema = proxy.app.openapi()
    assert schema == json.loads((FIXTURES / "headless_openapi.json").read_text())
    ids = []
    for key, metadata in OWNER_OPERATIONS.items():
        method, path = key.split(" ", 1)
        operation = schema["paths"][path][method.lower()]
        mode = operation_security.OWNER_BEARER if metadata.get("bearer_only") else operation_security.OWNER
        assert operation["security"] == operation_security.security_for(mode), key
        if metadata.get("bearer_only"):
            assert "A browser session is refused, even with CSRF and action headers." in operation["description"], key
        audience = OWNER_ONLY_TAG if metadata["owner_only"] else OWNER_AND_ADMINS_TAG
        assert operation["tags"] == [metadata["tag"], audience]
        path_names = set(re.findall(r"\{([^}]+)\}", path))
        assert path_names == {parameter["name"] for parameter in operation["parameters"]
                              if parameter["in"] == "path"}
        ids.append(operation["operationId"])
    assert len(ids) == len(set(ids))
    for row in INVENTORY:
        if row["auth"] == "session-only":
            assert row["method"].lower() not in schema["paths"].get(row["path"], {})


def test_openapi_declares_each_header_that_carries_an_app_key():
    schemes = proxy.app.openapi()["components"]["securitySchemes"]
    assert schemes["AppKeyBearer"]["type"] == "http"
    assert schemes["AppKeyBearer"]["scheme"] == "bearer"
    assert "trusted" in schemes["AppKeyBearer"]["description"]
    # The declared headers are the ones the proxy reads, and it reads no query key unless the owner turns that on.
    header_names = {scheme["name"] for scheme in schemes.values() if scheme.get("in") == "header"}
    assert header_names == set(runtime_auth.CLIENT_KEY_HEADERS)
    # The only query scheme is the signed link's: no App key is read from the query string.
    assert {name for name, scheme in schemes.items() if scheme.get("in") == "query"} == {operation_security.SIGNED_LINK_SCHEME}
    for header in header_names:
        assert proxy.get_client_api_key_from_headers({header: "app-key-fixture"}) == "app-key-fixture"
    assert proxy.get_client_api_key_from_headers({"authorization": "Bearer app-key-fixture"}) == "app-key-fixture"


def test_unsigned_reference_lists_every_path_of_the_schema():
    client = TestClient(proxy.app)
    paths = proxy.app.openapi()["paths"]
    for url in ("/docs", "/redoc"):
        response = client.get(url)
        assert response.status_code == 200, url
        for path in paths:
            assert f"<code>{escape(path)}</code>" in response.text, path


def _operations(schema):
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            yield path, method, operation


def _concrete(path):
    return re.sub(r"\{[^}]+\}", "missing-fixture-id", path)


def _operations_by_endpoint():
    """Every operation a frontend row mounts, keyed by the handler its route serves."""
    by_endpoint = {}
    for translator in proxy.FRONTEND_TRANSLATORS.ids():
        for operation in proxy.FRONTEND_TRANSLATORS.operations(translator).values():
            by_endpoint.setdefault(operation.endpoint, []).append(operation)
    return by_endpoint


def test_every_operation_in_the_reference_declares_its_security():
    missing = [f"{method.upper()} {path}" for path, method, operation in _operations(proxy.app.openapi())
               if "security" not in operation]
    assert not missing, missing


def test_every_self_authenticating_operation_declares_how_it_checks_its_caller():
    undeclared = []
    for translator in proxy.FRONTEND_TRANSLATORS.ids():
        for intent, operation in proxy.FRONTEND_TRANSLATORS.operations(translator).items():
            if operation.self_authenticates and getattr(operation, "auth", None) not in operation_security.SELF_AUTH_MODES:
                undeclared.append(f"{translator}/{intent}")
    assert not undeclared, undeclared


def test_every_mounted_operation_has_the_security_its_mount_predicate_implies():
    schema = proxy.app.openapi()
    by_endpoint = _operations_by_endpoint()
    checked = 0
    for route in proxy.app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        for method in sorted(route.methods):
            matching = [op for op in by_endpoint.get(route.endpoint, []) if method in op.methods]
            if not matching or method.lower() not in schema["paths"].get(route.path, {}):
                continue
            modes = {frontend_mount.operation_mode(op) for op in matching}
            assert len(modes) == 1, (method, route.path, modes)
            expected = [] if proxy.is_public_path(route.path) else operation_security.security_for(modes.pop())
            actual = schema["paths"][route.path][method.lower()].get("security")
            assert actual == expected, (method, route.path, actual)
            checked += 1
    assert checked >= 100, checked


def test_each_self_authenticating_operation_refuses_a_call_without_credentials(principals):
    client = principals.client
    client.cookies.clear()
    by_endpoint = _operations_by_endpoint()
    probed = set()
    for route in proxy.app.routes:
        methods = {"WEBSOCKET"} if isinstance(route, WebSocketRoute) else set(getattr(route, "methods", None) or ())
        for method in sorted(methods):
            selfauth = [op for op in by_endpoint.get(route.endpoint, []) if method in op.methods and op.self_authenticates]
            if not selfauth:
                continue
            mode = selfauth[0].auth
            if method == "WEBSOCKET":
                with pytest.raises(WebSocketDenialResponse) as denial:
                    with client.websocket_connect(route.path):
                        pytest.fail(f"{route.path} opened a session without credentials")
                status = denial.value.status_code
            else:
                body = {} if method in {"POST", "PUT", "PATCH"} else None
                status = client.request(method, _concrete(route.path), json=body, follow_redirects=False).status_code
            # An App key is refused with 401 or 403. A signed URL is refused without its signature (400 or 403), never served.
            refused = {operation_security.APP_KEY: {401, 403}, operation_security.SIGNED_URL: {400, 403}}[mode]
            assert status in refused, (method, route.path, mode, status)
            probed.add(route.endpoint)
    self_authenticating = {op.endpoint for ops in by_endpoint.values() for op in ops if op.self_authenticates}
    assert self_authenticating <= probed, sorted(endpoint.__name__ for endpoint in self_authenticating - probed)


# One operation per declared mode, probed with the credential its declaration names. Each probe reads, except the
# browser-session probe, which signs out a throwaway person (the principals fixture makes that person's session).
MODE_PROBES = {
    operation_security.OWNER: ("GET", "/v1/status"),
    operation_security.BROWSER_SESSION: ("POST", "/auth/api/sign-out-everywhere"),
    operation_security.OPTIONAL_SESSION: ("GET", "/auth/api/remembered"),
    operation_security.PUBLIC: ("GET", "/auth/api/options"),
    operation_security.SIGNED_URL: ("GET", "/v1/assets/{asset_id}/thumbnail"),
    operation_security.APP_KEY: ("GET", "/gateway/v1/aliases"),
    operation_security.APP_KEY_OR_SESSION: ("GET", "/gateway/v1/requests"),
}
# The refusal each mode gives a call with no credential. PUBLIC and OPTIONAL_SESSION admit such a call.
REFUSED = {
    operation_security.OWNER: {401, 403}, operation_security.BROWSER_SESSION: {401, 403},
    operation_security.APP_KEY: {401, 403}, operation_security.APP_KEY_OR_SESSION: {401, 403},
    operation_security.SIGNED_URL: {400, 403},
}
ASSET_ID = "img_" + "a" * 64


def _credential(case, mode, name):
    """The part of a request that proves one declared requirement, with the real credential that requirement names."""
    if name == "AppKeyBearer":
        return {"headers": {"Authorization": f"Bearer {case.app_key}"}}
    if name == "AppKeyApiKey":
        return {"headers": {"api-key": case.app_key}}
    if name == "AppKeyXApiKey":
        return {"headers": {"x-api-key": case.app_key}}
    if name == "OwnerBearer":
        return {"headers": case._auth()}
    if name == "BrowserSession":
        # An owner operation takes the owner's session; the sign-out takes a throwaway person's; the rest take a member's.
        cookie, headers = {operation_security.OWNER: (case.owner_cookie, case.owner_headers),
                           operation_security.BROWSER_SESSION: (case.probe_cookie, case.probe_headers)}.get(
            mode, (case.member_cookie, case.member_headers))
        return {"cookie": cookie, "headers": headers}
    if name == operation_security.SIGNED_LINK_SCHEME:
        expires = int(time.time()) + 600
        return {"query": {"expires": expires, "sig": proxy._sign_asset_token(ASSET_ID, expires, thumbnails.THUMB_MODE)}}
    raise AssertionError(f"no credential for {name}")


@pytest.mark.parametrize("mode", list(MODE_PROBES))
def test_each_declared_mode_is_enforced_as_declared(mode, principals):
    """A call with no credential is refused, or admitted for PUBLIC and OPTIONAL_SESSION. A call with each declared
    credential gets past authentication: not 401 or 403."""
    method, template = MODE_PROBES[mode]
    declared = proxy.app.openapi()["paths"][template][method.lower()]["security"]
    assert declared == operation_security.security_for(mode)
    path = re.sub(r"\{[^}]+\}", ASSET_ID, template)
    body = {} if method == "POST" else None
    client = principals.client
    client.cookies.clear()
    anonymous = client.request(method, path, json=body, follow_redirects=False).status_code
    if mode in {operation_security.PUBLIC, operation_security.OPTIONAL_SESSION}:
        assert anonymous not in {401, 403}, (mode, anonymous)
    else:
        assert anonymous in REFUSED[mode], (mode, anonymous)
    for requirement in declared:
        for name in requirement:
            credential = _credential(principals, mode, name)
            client.cookies.clear()
            if "cookie" in credential:
                client.cookies.set(*credential["cookie"])
            response = client.request(method, path, json=body, headers=credential.get("headers", {}),
                                      params=credential.get("query"), follow_redirects=False)
            assert response.status_code not in {401, 403}, (mode, name, response.status_code, response.text[:200])


# With a valid expiry, the signature is what decides these refusals. For each operation a link reaches, this gives the
# refusal for a missing signature and the refusal for a wrong one, each as (status, message). The content routes say a
# signature is missing before they check any.
BAD_SIGNATURE = (403, "Invalid or expired signature")
UNSIGNED = (400, "Signature required")
SIGNED_LINK_REFUSALS = {
    "/v1/assets/{asset_id}/thumbnail": {"missing": BAD_SIGNATURE, "wrong": BAD_SIGNATURE},
    "/v1/assets/{asset_id}/content": {"missing": UNSIGNED, "wrong": BAD_SIGNATURE},
    "/v1/images/{image_id}/content": {"missing": UNSIGNED, "wrong": BAD_SIGNATURE},
    "/openai/v1/assets/{asset_id}/content": {"missing": UNSIGNED, "wrong": BAD_SIGNATURE},
    "/openai/v1/images/{image_id}/content": {"missing": UNSIGNED, "wrong": BAD_SIGNATURE},
}


def test_signed_link_refusals_cover_exactly_the_signed_operations():
    signed = {path for path, _method, operation in _operations(proxy.app.openapi())
              if {operation_security.SIGNED_LINK_SCHEME: []} in operation.get("security", [])}
    assert signed == set(SIGNED_LINK_REFUSALS)


@pytest.mark.parametrize("case", ["missing", "wrong"])
@pytest.mark.parametrize("path", list(SIGNED_LINK_REFUSALS))
def test_signed_link_refuses_a_missing_or_wrong_signature(path, case, principals):
    """A valid expiry with no signature, or with a signature that is not this link's, gets exactly its handler's refusal.
    The refusal comes before any asset lookup, so the asset does not need to exist."""
    query = {"expires": str(int(time.time()) + 600)}
    if case == "wrong":
        query["sig"] = "0" * 64  # shaped like an HMAC, and not this link's
    principals.client.cookies.clear()
    response = principals.client.get(re.sub(r"\{[^}]+\}", ASSET_ID, path), params=query, follow_redirects=False)
    assert (response.status_code, response.json()["error"]["message"]) == SIGNED_LINK_REFUSALS[path][case], (
        path, case, response.status_code, response.text[:200])


@pytest.mark.parametrize("path", list(SIGNED_LINK_REFUSALS))
def test_signed_link_with_its_own_signature_is_not_refused_for_the_signature(path, principals):
    """The same link with its own signature gets past the signature check, so the refusals above are the signature's."""
    expires = int(time.time()) + 600
    mode = thumbnails.THUMB_MODE if path.endswith("/thumbnail") else None
    query = {"expires": str(expires), "sig": proxy._sign_asset_token(ASSET_ID, expires, mode)}
    principals.client.cookies.clear()
    response = principals.client.get(re.sub(r"\{[^}]+\}", ASSET_ID, path), params=query, follow_redirects=False)
    assert response.status_code != 403 and "signature" not in response.text.lower(), (path, response.status_code)


# The request history takes an App key or the signed-in session of the person who made the request. The maker's
# delete-everything is left out of the admitted calls: it would empty that person's history.
REQUEST_HISTORY = [("GET", "/gateway/v1/requests"), ("GET", "/gateway/v1/requests/missing-fixture-id"),
                   ("DELETE", "/gateway/v1/requests/missing-fixture-id"), ("DELETE", "/gateway/v1/requests")]


@pytest.mark.parametrize("method,path", REQUEST_HISTORY, ids=[f"{m} {p}" for m, p in REQUEST_HISTORY])
def test_request_history_refuses_a_call_without_credentials(method, path, principals):
    principals.client.cookies.clear()
    assert principals.client.request(method, path, follow_redirects=False).status_code == 401


@pytest.mark.parametrize("method,path", REQUEST_HISTORY[:3], ids=[f"{m} {p}" for m, p in REQUEST_HISTORY[:3]])
def test_request_history_admits_the_makers_own_session(method, path, principals):
    principals.client.cookies.clear()
    principals.client.cookies.set(*principals.member_cookie)
    response = principals.client.request(method, path, headers=principals.member_headers, follow_redirects=False)
    assert response.status_code not in {401, 403}, (method, path, response.status_code)


def test_every_owner_operation_is_tagged_and_opens_with_its_audience_note():
    owner = [(path, method, operation) for path, method, operation in _operations(proxy.app.openapi())
             if {"OwnerBearer": []} in operation.get("security", [])]
    assert len(owner) >= len(OWNER_OPERATIONS)
    for path, method, operation in owner:
        owner_only = OWNER_OPERATIONS.get(f"{method.upper()} {path}", {}).get("owner_only", False)
        tag, note = (OWNER_ONLY_TAG, OWNER_ONLY_NOTE) if owner_only else (OWNER_AND_ADMINS_TAG, OWNER_AND_ADMINS_NOTE)
        audience_tags = [t for t in operation["tags"] if t in {OWNER_ONLY_TAG, OWNER_AND_ADMINS_TAG}]
        assert audience_tags == [tag], (method, path, audience_tags)
        assert operation["description"].startswith(note), (method, path)


def test_every_scheme_a_requirement_names_is_declared_in_components():
    schema = proxy.app.openapi()
    declared = set(schema["components"]["securitySchemes"])
    used = {name for _, _, operation in _operations(schema) for requirement in operation.get("security", [])
            for name in requirement}
    assert used <= declared, sorted(used - declared)
    assert {"AppKeyBearer", "AppKeyApiKey", "AppKeyXApiKey", "OwnerBearer", "BrowserSession", "SignedLink"} <= used


def test_public_paths_and_the_gateway_key_layer_have_the_security_the_gateway_enforces():
    for path, method, operation in _operations(proxy.app.openapi()):
        if proxy.is_public_path(path):
            assert operation["security"] == [], (method, path)
        elif path.startswith(proxy.KEY_LAYER_PREFIXES):
            mode = (operation_security.APP_KEY_OR_SESSION if operation_security.session_passes_key_layer(path)
                    else operation_security.APP_KEY)
            assert operation["security"] == operation_security.security_for(mode), (method, path)


def test_account_routes_under_me_stay_out_of_the_reference():
    assert not [path for path in proxy.app.openapi()["paths"] if path.startswith("/me/")]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def test_existing_data_plane_schema_is_byte_equivalent():
    """Digests were captured from the unmodified base commit, before the overlay.

    /v1/status is the one path re-captured since: it takes the owner and admins audience, so its tags and description note
    changed on purpose. Removing ace-step-1.5-2b-base (2026-10-26) re-captured the two audio.generate paths and
    AudioGenerationRequest, and dropped its input schema.
    """
    schema = proxy.app.openapi()
    baseline = json.loads((FIXTURES / "headless_data_plane_hashes.json").read_text())
    for path, expected in baseline["paths"].items():
        operation = copy.deepcopy(schema["paths"][path])
        for method_operation in operation.values():
            if isinstance(method_operation, dict):
                signed = {operation_security.SIGNED_LINK_SCHEME: []} in method_operation.get("security", [])
                # Each operation's security is checked by its own tests (API reference security), not by this digest.
                method_operation.pop("security", None)
                if signed and "parameters" in method_operation:
                    # A signed link's sig and expires parameters are documented overlay, like its security.
                    method_operation["parameters"] = [p for p in method_operation["parameters"]
                                                      if not (p.get("in") == "query" and p.get("name") in {"sig", "expires"})]
        if path == "/gateway/v1/media/jobs/{job_id}/events":
            # The per-job cursor is an additive opt-in; the legacy query and
            # every other part of the original operation remain unchanged.
            operation["get"]["parameters"] = [p for p in operation["get"]["parameters"]
                                                 if p["name"] != "after_sequence"]
        assert _digest(operation) == expected, path
    for name, expected in baseline["schemas"].items():
        assert _digest(schema["components"]["schemas"][name]) == expected, name


def _session_headers(session):
    """The headers a browser session carries on a write: the CSRF token and the action headers, as the member's do."""
    return {
        "Accept": "application/json",
        "x-azizam-csrf": csrf_token_for_session(session, signing_key=b"s" * 32),
        "x-azizam-admin-action": "1",
        "x-azizam-user-action": "1",
    }


@pytest.fixture(scope="module")
def principals():
    # Reuse the existing integration scaffolding and real SQLite admin-key mint.
    case = admin_integration.ProxyAdminIntegrationTests(methodName="runTest")
    case.setUp()
    try:
        accounts.initialize(case.db)
        providers.ensure_ledger(path=proxy.ADMIN_DB_PATH.parent / "provider_spend.db")
        receipt_store.init_db()
        accountable = admin_store.upsert_oidc_user("https://idp.example", "headless-accountable",
                                                  email="accountable@example.com", path=case.db)
        app = case.client.post("/admin/api/apps", headers=case._auth(), json={
            "name": "auth-fixture", "access_plan": "person", "owner_user_id": accountable["id"]})
        assert app.status_code == 201, app.text
        case.app_key = issue_app_key(case.db, app.json()["app"]["id"], label="auth-fixture")
        cookie = CookieConfig(secure=False)
        proxy.ADMIN_AUTH.signing_key = b"s" * 32
        proxy.ADMIN_AUTH.cookie = cookie
        member = admin_store.upsert_oidc_user("https://idp.example", "headless-member",
                                            email="member@example.com", path=case.db)
        # A current sign-in, not an 8-hour cookie from before sliding sessions: the first request with one of those
        # would upgrade it (a write), and the same cookie sent again after the grace window would end the sign-in.
        session = session_from_claims({"sub": member["id"], "email": member["email"]}, role="none", cookie=cookie,
                                      idle_seconds=30 * 86400, absolute_seconds=90 * 86400)
        case.member_cookie = (cookie.name, encode_session_cookie(session, signing_key=b"s" * 32))
        case.member_headers = {
            "Accept": "application/json",
            "x-azizam-csrf": csrf_token_for_session(session, signing_key=b"s" * 32),
            "x-azizam-admin-action": "1",
            "x-azizam-user-action": "1",
        }
        # The owner's session: the owner role is granted in the store, which the session's roles are read from.
        owner = admin_store.upsert_oidc_user("https://idp.example", "headless-owner", email="owner@example.com", path=case.db)
        admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=owner["id"], path=case.db)
        owner_session = session_from_claims({"sub": owner["id"], "email": owner["email"]}, role="owner", cookie=cookie,
                                            idle_seconds=30 * 86400, absolute_seconds=90 * 86400)
        case.owner_cookie = (cookie.name, encode_session_cookie(owner_session, signing_key=b"s" * 32))
        case.owner_headers = _session_headers(owner_session)
        # A person whose sign-in the sign-out probe ends. No other test signs this person in.
        probe = admin_store.upsert_oidc_user("https://idp.example", "headless-probe", email="probe@example.com", path=case.db)
        probe_session = session_from_claims({"sub": probe["id"], "email": probe["email"]}, role="none", cookie=cookie,
                                            idle_seconds=30 * 86400, absolute_seconds=90 * 86400)
        case.probe_cookie = (cookie.name, encode_session_cookie(probe_session, signing_key=b"s" * 32))
        case.probe_headers = _session_headers(probe_session)
        # Initialize the category store before measuring request-side effects.
        case.client.get("/admin/api/categories/history", headers=case._auth())
        yield case
    finally:
        case.tearDown()


# Valid typed bodies for the sign-in routes; each stops in the handler before any effect.
_SIGNIN_PROPOSAL = {"methods": {"password": True, "provider": False}, "provider": {},
                    "rules": {"fallback": "closed", "rules": []}}
SIGNIN_BODIES = {
    "/admin/api/signin/preview": {"proposal": _SIGNIN_PROPOSAL},
    "/admin/api/signin/discover": {"address": ""},
    "/admin/api/signin/test": {"provider": {}},
    "/admin/api/signin/link-owner": {"test_id": "missing-fixture-id"},
    "/admin/api/signin": {"proposal": _SIGNIN_PROPOSAL},
    "/admin/api/signin/owner-password": {"password": "short"},
}


def _request(row):
    path = re.sub(r"\{(?:reservation_id|event_id)\}", "1", row["path"])
    path = re.sub(r"\{[^}]+\}", "missing-fixture-id", path)
    # Supply valid typed bodies so the request reaches auth instead of 422.
    body = {"access_plan": "plan-missing"} if path == "/admin/api/invites" else {}
    body = SIGNIN_BODIES.get(path, body)
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


def _gateway_databases(root):
    """The gateway's databases under root. Pytest's own temporary tree (tmp/) holds every earlier test's
    databases; this probe reads the gateway's state, so it does not walk or dump that tree."""
    found = []
    for directory, dirnames, filenames in os.walk(root):
        if Path(directory) == root:
            dirnames[:] = [name for name in dirnames if name != "tmp"]
        found.extend(Path(directory) / name for name in filenames if name.endswith(".db"))
    return sorted(found)


def _database_contents(root):
    _settle_metrics_writer()
    result = {}
    for path in _gateway_databases(root):
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
        typed = row.get("handler", "").startswith(("gateway.accounts_api.", "gateway.signin_api."))
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
    "app_not_found", "trusted_address_not_found", "access_plan_not_found", "credential_not_found", "provider_not_found",
    "account_not_found", "invalid_defaults", "invite_not_found", "link_unavailable", "extension_package_not_found",
    "revision_not_found", "no_active_snapshot", "invalid_retention",
    "settings_revision_required", "registry_revision_required", "litellm_unconfigured",
    "generation_mismatch", "audit_event_not_found", "person_not_found",
    "invalid_instance", "invalid_prompt_memory", "invalid_sessions",
    "address_invalid", "client_id_required", "test_not_passed", "test_not_found", "owner_person_required",
    "invalid_password", "service_not_found", "record_not_found", "retention_destination_not_found",
    "ledger_not_found",
}


# The owner writes that refuse a browser session, with the body each is probed with and what each credential gets. The
# body is invalid on purpose: a session that got past the check would stop at the body check and change nothing.
BEARER_ONLY = {key for key, metadata in OWNER_OPERATIONS.items() if metadata.get("bearer_only")}
BEARER_ONLY_PROBES = {
    "POST /admin/api/drain": {"body": {"draining": "not-a-boolean"},
                              "session": (403, "drain_bearer_required"), "bearer": (400, "invalid_draining")},
}


def test_bearer_only_probes_cover_exactly_the_routes_the_route_map_flags():
    assert set(BEARER_ONLY_PROBES) == BEARER_ONLY


@pytest.mark.parametrize("key", sorted(BEARER_ONLY_PROBES))
def test_bearer_only_route_refuses_the_owner_session_and_admits_the_owner_bearer(key, principals):
    probe = BEARER_ONLY_PROBES[key]
    method, path = key.split(" ", 1)
    drain_before = _drain_state()
    principals.client.cookies.clear()
    principals.client.cookies.set(*principals.owner_cookie)
    session = principals.client.request(method, path, json=probe["body"], headers=principals.owner_headers,
                                        follow_redirects=False)
    assert (session.status_code, session.json()["error"]["code"]) == probe["session"], (key, session.text[:200])
    principals.client.cookies.clear()
    bearer = principals.client.request(method, path, json=probe["body"], headers=principals._auth(),
                                       follow_redirects=False)
    assert (bearer.status_code, bearer.json()["error"]["code"]) == probe["bearer"], (key, bearer.text[:200])
    assert _drain_state() == drain_before


def _production_modules():
    """The modules that define the gateway's routes and the checks those routes make."""
    root = Path(proxy.__file__).parent
    return [root / "proxy.py", *sorted((root / "gateway").rglob("*.py"))]


class _ReferencesAndChecks(ast.NodeVisitor):
    """Per function: the names it refers to (called, passed on or read), and whether it compares an auth method with "bearer"."""

    def __init__(self):
        self.scope = []
        self.refers = {}
        self.refusing = set()

    def _function(self, node):
        self.scope.append(node.name)
        self.refers.setdefault(node.name, set())
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_Name(self, node):
        if self.scope:
            self.refers[self.scope[-1]].add(node.id)

    def visit_Attribute(self, node):
        if self.scope:
            self.refers[self.scope[-1]].add(node.attr)
        self.generic_visit(node)

    def visit_Compare(self, node):
        if (self.scope and any(isinstance(right, ast.Constant) and right.value == "bearer" for right in node.comparators)
                and "auth_method" in ast.unparse(node.left)):
            self.refusing.add(self.scope[-1])
        self.generic_visit(node)


@functools.cache
def _bearer_refusing_handlers():
    """The functions that refuse every credential but a bearer key, and every function that refers to one of them.

    A handler passes its check on as an argument (``asyncio.to_thread(_drain_authorize, ...)``), so a reference counts.
    """
    scan = _ReferencesAndChecks()
    for module in _production_modules():
        scan.visit(ast.parse(module.read_text()))
    handlers = set(scan.refusing)
    while True:
        referrers = {name for name, refers in scan.refers.items() if refers & handlers}
        if referrers <= handlers:
            return frozenset(handlers)
        handlers |= referrers


def test_every_handler_that_refuses_a_browser_session_write_is_flagged_bearer_only():
    handlers = _bearer_refusing_handlers()
    # The scan has to find the drain's check and the drain's write handler, or the comparison below would pass with nothing found.
    assert {"_drain_authorize", "admin_drain_set"} <= handlers
    # The check guards writes: the drain's GET reaches the same helper with write off, so sessions still read it.
    writes = {f"{method} {path}" for path, route in walk_routes(proxy.app.routes) if isinstance(route, APIRoute)
              for method in route.methods - {"GET", "HEAD", "OPTIONS"} if route.endpoint.__name__ in handlers}
    assert writes == BEARER_ONLY, sorted(writes ^ BEARER_ONLY)


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

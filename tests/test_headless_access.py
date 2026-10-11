"""Registered-route coverage, schema stability, and real credential boundaries."""
from __future__ import annotations

import hashlib
import copy
import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute

import proxy
from gateway import admin_store
from gateway.admin_api import AdminAuthError
from gateway.admin_oidc import CookieConfig, encode_session_cookie, session_from_claims
from gateway.owner_openapi import OWNER_OPERATIONS
import test_proxy_admin_integration as admin_integration

FIXTURES = Path(__file__).parent / "fixtures"
INVENTORY = json.loads((FIXTURES / "headless_routes.json").read_text())
OWNER_ROUTES = [row for row in INVENTORY if row["auth"] == "owner-bearer"]


def _inventoried(path):
    return path.startswith(("/admin", "/me", "/auth", "/setup")) or path in {
        "/status", "/status/", "/status/raw", "/status/raw/", "/logout", "/login"}


def test_inventory_covers_registered_routes_and_documentation():
    actual = {(method, route.path, route.endpoint.__module__ + "." + route.name)
              for route in proxy.app.routes if isinstance(route, APIRoute) and _inventoried(route.path)
              for method in route.methods}
    assert actual == {(r["method"], r["path"], r["handler"]) for r in INVENTORY}
    documented = set(re.findall(r"\| `(GET|POST|PUT|PATCH|DELETE|HEAD) ([^`]+)` \|", (
        Path(__file__).parents[1] / "docs/ops/HEADLESS_ACCESS.md").read_text()))
    assert documented == {(r["method"], r["path"]) for r in INVENTORY}
    assert set(OWNER_OPERATIONS) == {
        f'{r["method"]} {r["path"]}' for r in OWNER_ROUTES if r["path"].startswith("/admin/api/")}


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


def _database_contents(root):
    result = {}
    for path in sorted(root.rglob("*.db")):
        with sqlite3.connect(path) as connection:
            result[str(path)] = _digest(list(connection.iterdump()))
    return result


@pytest.mark.parametrize("row", OWNER_ROUTES, ids=lambda row: row["method"] + " " + row["path"])
def test_every_owner_route_rejects_app_and_member_without_effects(row, principals, gateway_test_state_dir):
    case = principals
    path, body = _request(row)
    before = _database_contents(gateway_test_state_dir)
    fixture_before = _database_contents(case.db.parent)
    drain_before = proxy.DRAIN_STATE.snapshot()
    config_before = copy.deepcopy(proxy.config)
    for principal in ("app", "member"):
        case.client.cookies.clear()
        headers = {"Authorization": f"Bearer {case.app_key}"} if principal == "app" else {"Accept": "application/json"}
        if principal == "member":
            case.client.cookies.set(*case.member_cookie)
        response = case.client.request(row["method"], path, json=body, headers=headers, follow_redirects=False)
        if not path.startswith("/admin/api/") and response.status_code == 302:
            # HTML entry points deny by redirecting members to their own portal.
            assert principal == "member"
            assert response.headers["location"] == "/"
        else:
            assert response.status_code in {401, 403}, (row["method"], row["path"], principal, response.status_code)
        assert _database_contents(gateway_test_state_dir) == before
        assert _database_contents(case.db.parent) == fixture_before
        assert proxy.DRAIN_STATE.snapshot() == drain_before
        assert proxy.config == config_before


@pytest.mark.parametrize("row", OWNER_ROUTES, ids=lambda row: row["method"] + " " + row["path"])
def test_every_owner_route_accepts_real_owner_credential_at_auth_boundary(row, principals):
    """Stop immediately after real credential validation, before operational effects.

    Full mutations (provider probes, drain, reload, erasure) must not run merely
    to check bearer reachability. Successful user workflows have separate tests.
    """
    original = type(proxy.ADMIN_AUTH).__call__

    def checkpoint(auth, request):
        context = original(auth, request)
        assert "owner" in context.roles and context.auth_method == "bearer"
        raise AdminAuthError("Owner credential accepted", status_code=299, code="test_owner_accepted")

    path, body = _request(row)
    principals.client.cookies.clear()
    with patch.object(type(proxy.ADMIN_AUTH), "__call__", checkpoint):
        response = principals.client.request(row["method"], path, json=body,
            headers=principals._auth(), follow_redirects=False)
    assert response.status_code == 299, (row["method"], row["path"], response.status_code)
    assert response.json()["error"]["code"] == "test_owner_accepted"


@pytest.mark.parametrize("row", [row for row in INVENTORY if row["auth"] == "session-only"],
                         ids=lambda row: row["method"] + " " + row["path"])
def test_session_only_routes_do_not_accept_standalone_owner_bearer(row, principals):
    principals.client.cookies.clear()
    path, body = _request(row)
    response = principals.client.request(row["method"], path, json=body,
        headers=principals._auth(), follow_redirects=False)
    assert response.status_code in {401, 403, 302, 303}, (row["method"], row["path"], response.status_code)
    if response.is_redirect:
        assert response.headers["location"] in {"/setup", "/ui/sign-in/"}


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

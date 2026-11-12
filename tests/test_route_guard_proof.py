"""Every mutating owner, member, account and setup route refuses what it should, and a removed guard fails here.

tests/test_headless_access.py already proves that an App key and a member session are refused by the owner routes.
This file closes the rest of the table: a call with no credential, a signed-in administrator who is not the owner on
the owner-only routes, and an App key on the routes that take a browser session. It reads the same route inventory
(tests/fixtures/headless_routes.json), so a new mutating route has to be named in one class below or the
classification test fails.

Each refusal also leaves every database, the drain state and the config exactly as they were.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import proxy
from gateway import admin_store
from gateway.admin_oidc import CookieConfig, csrf_token_for_session, encode_session_cookie, session_from_claims
from tests.test_headless_access import (  # noqa: F401  (principals is a fixture)
    INVENTORY, _database_contents, _drain_state, _request, _session_headers, principals,
)

PREFIXES = ("/admin/api", "/me/api", "/auth/api", "/setup")
WRITES = [row for row in INVENTORY if row["method"] not in {"GET", "HEAD", "OPTIONS"} and row["path"].startswith(PREFIXES)]


def _key(row):
    return f"{row['method']} {row['path']}"


# Public or credential-bearing entry points: each is reachable with no sign-in because the request itself carries the
# proof (a password, an invitation link, a reset token, a first-run claim). A route is named here with the reason, or
# it is in one of the guarded classes below.
PUBLIC_ENTRY = {
    "POST /auth/api/login": "the sign-in itself: the body carries the password",
    "POST /auth/api/invite": "looks up an invitation link by its token (gateway/accounts_api.py inspect); read-only, the link is the credential",
    "POST /auth/api/invite/accept": "the holder of an invitation link; the link is the credential",
    "POST /auth/api/password-reset": "the holder of a reset link; the link is the credential",
    "POST /setup/claim": "first-run claim: needs an active admin key (checked in the handler) or a first-run instance",
    "POST /setup/local-owner": "first-run owner creation: first-run and client-address gates, not a sign-in",
    "POST /setup/local-login": "the local owner's sign-in itself: the body carries the password",
    "POST /auth/api/clear-browser-cache": "clears the caller's own browser state; touches no stored data",
    "DELETE /auth/api/remembered/{tile}": "forgets a name in the caller's own browser cookie; touches no stored data",
}

OWNER_ROUTES = [row for row in WRITES if row["auth"] == "owner-bearer"]
OWNER_ONLY = [row for row in OWNER_ROUTES if "owner role required" in row["gate"]]
SESSION_ROUTES = [row for row in WRITES if row["auth"] == "session-only" or _key(row) in {
    "POST /auth/api/reauth", "POST /auth/api/sign-out-everywhere"}]


def test_every_mutating_route_is_in_exactly_one_class():
    public = {_key(row) for row in WRITES if _key(row) in PUBLIC_ENTRY}
    owner = {_key(row) for row in OWNER_ROUTES}
    session = {_key(row) for row in SESSION_ROUTES}
    assert set(PUBLIC_ENTRY) == public, sorted(set(PUBLIC_ENTRY) ^ public)
    assert not (public & owner) and not (public & session) and not (owner & session)
    unclassified = {_key(row) for row in WRITES} - public - owner - session
    assert not unclassified, f"name each new mutating route in a class (and say why if it is public): {sorted(unclassified)}"
    assert all(PUBLIC_ENTRY.values())


@pytest.fixture(scope="module")
def admin_principal(principals):
    """A signed-in administrator who is not the owner."""
    case = principals
    cookie = CookieConfig(secure=False)
    person = admin_store.upsert_oidc_user("https://idp.example", "guard-proof-admin", email="admin-proof@example.com", path=case.db)
    admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=person["id"], path=case.db)
    session = session_from_claims({"sub": person["id"], "email": person["email"]}, role="admin", cookie=cookie,
                                  idle_seconds=30 * 86400, absolute_seconds=90 * 86400)
    case.admin_cookie = (cookie.name, encode_session_cookie(session, signing_key=b"s" * 32))
    case.admin_headers = _session_headers(session)
    return case


def _state(case, state_dir):
    """Every database except the request metrics (a refused call is counted there, and that grants nothing), the drain
    state and the config."""
    def contents(root):
        return {path: digest for path, digest in _database_contents(root).items() if not path.endswith("/metrics.db")}
    return contents(state_dir), contents(case.db.parent), _drain_state(), copy.deepcopy(proxy.config)


@pytest.fixture(autouse=True)
def _app_key_already_used(principals):
    """The first call with an App key stamps the key's last-used time. Do that before any snapshot is taken."""
    principals.client.cookies.clear()
    principals.client.get("/v1/models", headers={"Authorization": f"Bearer {principals.app_key}"})


class _Unchanged:
    """The databases, drain state and config before and after a refused call."""

    def __init__(self, case, state_dir):
        self.case, self.state_dir = case, state_dir

    def __enter__(self):
        self.before = _state(self.case, self.state_dir)
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            assert _state(self.case, self.state_dir) == self.before, "a refused request changed state"


# Routes whose body is read (and a wrong shape refused with 422) before the credential is looked at.
BODIES = {"POST /auth/api/reauth": {"password": "not-the-password"}}


def _send(case, row, *, headers=None, cookie=None):
    path, body = _request(row)
    body = BODIES.get(_key(row), body)
    case.client.cookies.clear()
    if cookie:
        case.client.cookies.set(*cookie)
    return case.client.request(row["method"], path, json=body, headers=headers or {"Accept": "application/json"},
                               follow_redirects=False)


def _refused(response, row):
    if response.status_code in {302, 303}:
        assert response.headers["location"] in {"/", "/setup/", "/sign-in/"}, (_key(row), response.headers["location"])
    else:
        assert response.status_code in {401, 403}, (_key(row), response.status_code, response.text[:200])


@pytest.mark.parametrize("row", OWNER_ROUTES + SESSION_ROUTES, ids=_key)
def test_a_call_with_no_credential_is_refused_and_changes_nothing(row, principals, gateway_test_state_dir):
    with _Unchanged(principals, gateway_test_state_dir):
        _refused(_send(principals, row), row)


@pytest.mark.parametrize("row", SESSION_ROUTES, ids=_key)
def test_an_app_key_is_refused_by_a_route_that_takes_a_browser_session(row, principals, gateway_test_state_dir):
    with _Unchanged(principals, gateway_test_state_dir):
        _refused(_send(principals, row, headers={"Authorization": f"Bearer {principals.app_key}"}), row)


# Owner-only routes whose owner check sits below the route's own lookups, so the probe has to reach it with a body that
# gets that far. Each names the refusal an administrator who is not the owner gets.
ADMIN_PROBES = {
    # An administrator may mint an admin key; only the owner may mint an owner key.
    "POST /admin/api/admin-keys": {"body": {"name": "guard-proof", "role": "owner"}, "code": "admin_role_forbidden"},
    # Only the App's accountable person may trust an address, and the App is not this administrator's.
    "POST /admin/api/apps/{app_id}/trusted-addresses": {"body": {"address": "10.9.9.9"}, "code": "not_your_app", "app": True},
    # Only the owner changes a secret (the check is before the registry generation check).
    "POST /admin/api/operator/services/{service_id}/secret-entries": {
        "body": {"commands": [{"operation": "add", "carrier": "config.api_key", "name": "k", "value": "v"}]},
        "headers": {"If-Match": '"os-0"'}, "code": "owner_required"},
}


def test_the_probes_name_owner_only_routes():
    assert set(ADMIN_PROBES) <= {_key(row) for row in OWNER_ONLY}


@pytest.mark.parametrize("row", OWNER_ONLY, ids=_key)
def test_an_administrator_who_is_not_the_owner_is_refused_by_an_owner_only_route(row, admin_principal, gateway_test_state_dir):
    case = admin_principal
    probe = ADMIN_PROBES.get(_key(row))
    with _Unchanged(case, gateway_test_state_dir):
        if probe is None:
            response = _send(case, row, headers=case.admin_headers, cookie=case.admin_cookie)
        else:
            path = row["path"]
            if probe.get("app"):
                app_id = case.client.get("/admin/api/apps", headers=case._auth()).json()["apps"][0]["id"]
                path = path.replace("{app_id}", app_id)
            path = path.replace("{service_id}", "missing-fixture-id")
            case.client.cookies.clear()
            case.client.cookies.set(*case.admin_cookie)
            response = case.client.request(row["method"], path, json=probe["body"],
                                           headers={**case.admin_headers, **probe.get("headers", {})}, follow_redirects=False)
            assert response.json()["error"]["code"] == probe["code"], (_key(row), response.status_code, response.text[:200])
        assert response.status_code in {302, 403}, (_key(row), response.status_code, response.text[:200])

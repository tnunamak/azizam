"""The MCP consent route builds its write's sign-in check from the session object the cookie lookup returns.

``BrowserSession.of`` reads ``session_id`` and ``family_id``, so a lookup that answers with a bare person id
broke every consent with a 500. ``tests/test_mcp_oauth.py`` hides that behind a ``str`` subclass; the cases here
use the real session class and the fixture of the conformance script, and drive the real consent route.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import accounts, admin_store, mcp_oauth
from gateway.admin_oidc import AdminSession
from gateway.mcp_oauth import McpOAuth, McpOAuthDeps
from gateway.mcp_oauth_store import Store
from gateway.session_guard import BrowserSession

BASE = "https://gw.test"
CALLBACK = "https://client.example/callback"
VERIFIER = "v" * 64


def _authorize(client: TestClient, resource: str) -> str:
    registered = client.post("/oauth/register", json={
        "client_name": "App", "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"]})
    assert registered.status_code == 201, registered.text
    started = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": registered.json()["client_id"], "redirect_uri": CALLBACK,
        "state": "st", "code_challenge": create_s256_code_challenge(VERIFIER), "code_challenge_method": "S256",
        "resource": resource, "scope": "mcp offline_access"})
    assert started.status_code == 302, started.text
    return parse_qs(urlsplit(started.headers["location"]).query)["request"][0]


@pytest.fixture()
def admin_session_app(tmp_path):
    """The consent routes behind the real ``AdminSession`` class, as ``proxy.py`` wires them."""
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    accounts.initialize(db)
    store = Store(lambda: db)
    session = AdminSession(subject="usr_a", email="a@example.org", role="member", issued_at=0.0,
                           expires_at=4_000_000_000.0, session_id="sid-1")
    oauth = McpOAuth(McpOAuthDeps(
        public_base_url=lambda request: BASE,
        session=lambda request: session if request.headers.get("x-test-person") == "usr_a" else None,
        person=lambda found: {"id": found.subject, "email": found.email},
        check_action=lambda request, found: None,
        person_can_connect=lambda person_id: True,
        plan=lambda person_id: {"name": "Household"},
        client_ip=lambda request: "203.0.113.9",
        fetch_client_document=None,
        store=store,
    ))
    app = FastAPI()
    oauth.register(app)
    return TestClient(app, base_url=BASE, follow_redirects=False), store


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_consent_with_a_real_session_object(admin_session_app, decision):
    client, store = admin_session_app
    request_id = _authorize(client, f"{BASE}/mcp")
    decided = client.post(f"/me/api/mcp/requests/{request_id}", json={"decision": decision},
                          headers={"x-test-person": "usr_a"})
    assert decided.status_code == 200, decided.text
    query = parse_qs(urlsplit(decided.json()["redirect"]).query)
    assert ("code" in query) == (decision == "allow")
    assert ("error" in query) == (decision == "deny")
    assert len(store.grants_for_person("usr_a")) == (1 if decision == "allow" else 0)


def _load_conformance_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "mcp_conformance.py"
    spec = importlib.util.spec_from_file_location("mcp_conformance_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_conformance_fixture_consent_route_answers(tmp_path, decision):
    script = _load_conformance_script()
    base = "http://127.0.0.1:1"
    client = TestClient(script.build_app(base, tmp_path / "admin.db"), base_url=base, follow_redirects=False)
    request_id = _authorize(client, f"{base}/mcp")
    decided = client.post(f"/me/api/mcp/requests/{request_id}", json={"decision": decision},
                          headers={script.SESSION_HEADER: script.PERSON})
    assert decided.status_code == 200, decided.text
    assert ("code" in decided.json()["redirect"]) == (decision == "allow")


@pytest.mark.parametrize("not_a_session", ["usr_a", None, 7, object()])
def test_browser_session_of_refuses_what_is_not_a_session(not_a_session):
    with pytest.raises(TypeError, match="BrowserSession.of"):
        BrowserSession.of(not_a_session, "usr_a")


def test_browser_session_of_reads_a_real_session():
    session = AdminSession(subject="usr_a", email="a@example.org", role="member", issued_at=0.0,
                           expires_at=10.0, session_id="sid-2", family="fam-1", absolute_expires_at=20.0)
    assert BrowserSession.of(session, "usr_a") == BrowserSession("usr_a", "sid-2", "fam-1", 10.0, 20.0)

"""MCP OAuth authorization server (gateway/mcp_oauth.py): discovery, registration,
PKCE, resource binding, consent, rotation, replay and revocation, end to end over HTTP."""

from __future__ import annotations

import secrets
import sqlite3
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from authlib.oauth2.rfc8414 import AuthorizationServerMetadata
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import mcp_oauth
from gateway.mcp_oauth import McpOAuth, McpOAuthDeps
from gateway.mcp_oauth_store import Store

BASE = "https://gw.test"
RESOURCE = f"{BASE}/mcp"
CALLBACK = "https://client.example/callback"
CIMD_ID = "https://client.example/oauth/client.json"


class World:
    def __init__(self, tmp_path) -> None:
        self.db = tmp_path / "admin.db"
        self.now = 1_800_000_000.0
        self.store = Store(lambda: self.db, clock=lambda: self.now)
        self.people = {"usr_a": {"id": "usr_a", "email": "a@example.org", "display_name": "Ann"},
                       "usr_b": {"id": "usr_b", "email": "b@example.org", "display_name": "Bo"}}
        self.blocked: set[str] = set()
        self.documents: dict[str, Any] = {}
        self.fetches: list[str] = []
        self.audits: list[tuple[str, dict]] = []
        self.oauth = McpOAuth(McpOAuthDeps(
            public_base_url=lambda r: BASE,
            session=lambda r: r.headers.get("x-test-person") or None,
            person=lambda session: self.people.get(session),
            check_action=lambda r, s: None if r.headers.get("x-test-csrf") == "ok" else JSONResponse(
                {"error": {"code": "user_csrf_required"}}, status_code=403),
            person_can_connect=lambda pid: pid in self.people and pid not in self.blocked,
            plan=lambda pid: {"name": "Household", "description": "Images and speech"},
            client_ip=lambda r: "203.0.113.9",
            fetch_client_document=self.fetch,
            store=self.store,
            audit=lambda action, **kw: self.audits.append((action, kw)),
        ))
        app = FastAPI()
        self.oauth.register(app)

        @app.get("/probe")
        async def probe(request: Request):
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            found = self.oauth.verify_access_token(token, RESOURCE)
            return {"person": found.person_id if found else None}

        self.client = TestClient(app, base_url=BASE, follow_redirects=False)

    async def fetch(self, url: str):
        self.fetches.append(url)
        doc = self.documents.get(url)
        if doc is None:
            raise OSError("unreachable")
        if isinstance(doc, bytes):
            return doc, {}
        import json
        return json.dumps(doc).encode(), {"cache-control": "max-age=600"}

    # ---- helpers

    def register(self, **metadata) -> dict:
        body = {"client_name": "Test App", "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], **metadata}
        resp = self.client.post("/oauth/register", json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def authorize(self, client_id: str, *, verifier: str | None = None, browser: TestClient | None = None,
                  **overrides) -> Any:
        verifier = verifier or secrets.token_urlsafe(48)
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "st-1",
                  "code_challenge": create_s256_code_challenge(verifier), "code_challenge_method": "S256",
                  "resource": RESOURCE, "scope": "mcp offline_access"}
        params.update(overrides)
        params = {k: v for k, v in params.items() if v is not None}
        return (browser or self.client).get("/oauth/authorize", params=params)

    def consent(self, resp, *, person="usr_a", decision="allow", browser: TestClient | None = None) -> Any:
        assert resp.status_code == 302, resp.text
        request_id = parse_qs(urlsplit(resp.headers["location"]).query)["request"][0]
        return (browser or self.client).post(f"/me/api/mcp/requests/{request_id}", json={"decision": decision},
                                             headers={"x-test-person": person, "x-test-csrf": "ok"})

    def code_for(self, client_id: str, verifier: str, **kw) -> str:
        decided = self.consent(self.authorize(client_id, verifier=verifier), **kw)
        assert decided.status_code == 200, decided.text
        query = parse_qs(urlsplit(decided.json()["redirect"]).query)
        return query["code"][0]

    def exchange(self, client_id: str, code: str, verifier: str, **extra) -> Any:
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "client_id": client_id,
                "code_verifier": verifier, "resource": RESOURCE, **extra}
        return self.client.post("/oauth/token", data={k: v for k, v in form.items() if v is not None})

    def connect(self, client_id: str | None = None, **kw) -> tuple[str, dict]:
        client_id = client_id or self.register()["client_id"]
        verifier = secrets.token_urlsafe(48)
        token = self.exchange(client_id, self.code_for(client_id, verifier, **kw), verifier)
        assert token.status_code == 200, token.text
        return client_id, token.json()

    def refresh(self, client_id: str, refresh_token: str, **extra) -> Any:
        return self.client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                                      "client_id": client_id, **extra})

    def person_for(self, access_token: str) -> str | None:
        return self.client.get("/probe", headers={"authorization": f"Bearer {access_token}"}).json()["person"]


@pytest.fixture()
def world(tmp_path) -> World:
    return World(tmp_path)


# ------------------------------------------------------------------ discovery


def test_metadata_is_valid_rfc8414_and_advertises_only_what_is_built(world):
    body = world.client.get("/.well-known/oauth-authorization-server").json()
    AuthorizationServerMetadata(body).validate()
    assert body["issuer"] == BASE
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert body["response_types_supported"] == ["code"]
    assert body["client_id_metadata_document_supported"] is True
    assert body["authorization_response_iss_parameter_supported"] is True
    assert body["registration_endpoint"] == f"{BASE}/oauth/register"
    assert "none" in body["token_endpoint_auth_methods_supported"]
    assert "private_key_jwt" not in body["token_endpoint_auth_methods_supported"]


def test_oauth_is_not_offered_without_a_secure_public_address(tmp_path):
    world = World(tmp_path)
    world.oauth.deps.public_base_url = lambda r: "http://192.168.1.20:5000"
    assert world.client.get("/.well-known/oauth-authorization-server").status_code == 404
    assert world.client.get("/oauth/authorize", params={"client_id": "x"}).status_code == 404
    world.oauth.deps.public_base_url = lambda r: "http://127.0.0.1:5000"
    assert world.client.get("/.well-known/oauth-authorization-server").status_code == 200


def test_cors_preflight_lets_browser_clients_reach_the_token_endpoint(world):
    resp = world.client.options("/oauth/token", headers={"origin": "http://localhost:6274",
                                                          "access-control-request-method": "POST"})
    assert resp.status_code == 204 and resp.headers["access-control-allow-origin"] == "*"


# ---------------------------------------------------------- registration (DCR)


def test_public_registration_returns_no_secret(world):
    body = world.register()
    assert body["client_id"].startswith(mcp_oauth.CLIENT_ID_PREFIX)
    assert "client_secret" not in body


def test_confidential_registration_gets_a_secret_stored_only_as_a_hash(world):
    body = world.register(token_endpoint_auth_method="client_secret_post")
    secret = body["client_secret"]
    assert secret and world.store.client(body["client_id"]).secret_hash != secret
    verifier = secrets.token_urlsafe(48)
    code = world.code_for(body["client_id"], verifier)
    assert world.exchange(body["client_id"], code, verifier).json()["error"] == "invalid_client"
    wrong = world.exchange(body["client_id"], world.code_for(body["client_id"], verifier), verifier,
                           client_secret="nope")
    assert wrong.json()["error"] == "invalid_client"
    ok = world.exchange(body["client_id"], world.code_for(body["client_id"], verifier), verifier, client_secret=secret)
    assert ok.status_code == 200, ok.text


def test_confidential_client_may_authenticate_with_http_basic(world):
    body = world.register(token_endpoint_auth_method="client_secret_basic")
    verifier = secrets.token_urlsafe(48)
    code = world.code_for(body["client_id"], verifier)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier}
    ok = world.client.post("/oauth/token", data=form, auth=(body["client_id"], body["client_secret"]))
    assert ok.status_code == 200, ok.text


@pytest.mark.parametrize("uri", ["http://evil.example/cb", "javascript:alert(1)", "https://x.example/cb#frag",
                                 "https://user@x.example/cb", "data:text/html,hi"])
def test_registration_refuses_unsafe_redirect_uris(world, uri):
    resp = world.client.post("/oauth/register", json={"client_name": "x", "redirect_uris": [uri],
                                                       "token_endpoint_auth_method": "none"})
    assert resp.status_code == 400
    assert resp.json()["error"] in {"invalid_redirect_uri", "invalid_client_metadata"}


@pytest.mark.parametrize("uri", ["http://127.0.0.1:33418/cb", "http://localhost/callback", "cursor://anysphere/cb",
                                 "com.example.app:/oauth"])
def test_registration_accepts_native_redirects(world, uri):
    assert world.register(redirect_uris=[uri])["redirect_uris"] == [uri]


def test_registration_is_rate_limited(world):
    world.oauth.register_limit = mcp_oauth._RateLimit(2, 3600)
    world.register()
    world.register()
    assert world.client.post("/oauth/register", json={"redirect_uris": [CALLBACK]}).status_code == 429


# ------------------------------------------------------------- the main flow


def test_full_flow_issues_tokens_bound_to_the_person_and_resource(world):
    client_id = world.register()["client_id"]
    verifier = secrets.token_urlsafe(48)
    started = world.authorize(client_id, verifier=verifier)
    assert started.status_code == 302 and started.headers["location"].startswith("/authorize/?request=")
    assert mcp_oauth.BINDING_COOKIE in started.cookies
    request_id = parse_qs(urlsplit(started.headers["location"]).query)["request"][0]
    details = world.client.get(f"/me/api/mcp/requests/{request_id}", headers={"x-test-person": "usr_a"}).json()
    assert details["client"]["name"] == "Test App"
    assert details["client"]["registration"] == "self_registered" and details["client"]["verified_domain"] is None
    assert details["client"]["redirect_host"] == "client.example"
    assert details["person"]["email"] == "a@example.org" and details["plan"]["name"] == "Household"

    decided = world.consent(started)
    redirect = decided.json()["redirect"]
    assert redirect.startswith(CALLBACK + "?")
    query = parse_qs(urlsplit(redirect).query)
    assert query["state"] == ["st-1"] and query["iss"] == [BASE]

    token = world.exchange(client_id, query["code"][0], verifier)
    assert token.status_code == 200, token.text
    assert token.headers["cache-control"] == "no-store"
    body = token.json()
    assert body["token_type"] == "Bearer" and body["access_token"].startswith(mcp_oauth.ACCESS_TOKEN_PREFIX)
    assert body["scope"] == "mcp offline_access" and body["expires_in"] == 3600
    assert world.person_for(body["access_token"]) == "usr_a"
    assert world.oauth.verify_access_token(body["access_token"], "https://other.test/mcp") is None
    assert world.oauth.verify_access_token(body["access_token"], "HTTPS://GW.TEST/mcp/") is not None
    assert ("mcp.connection.approved", {"person_id": "usr_a", "client_id": client_id}) in world.audits


def test_a_request_without_redirect_uri_needs_none_at_the_token_endpoint(world):
    client_id = world.register()["client_id"]
    verifier = secrets.token_urlsafe(48)
    decided = world.consent(world.authorize(client_id, verifier=verifier, redirect_uri=None))
    assert decided.json()["redirect"].startswith(CALLBACK + "?")
    code = parse_qs(urlsplit(decided.json()["redirect"]).query)["code"][0]
    assert world.exchange(client_id, code, verifier, redirect_uri=None).status_code == 200


def test_removed_person_loses_every_connection(world):
    _, first = world.connect()
    _, second = world.connect()
    assert world.store.revoke_person_grants("usr_a", "person_removed") == 2
    assert world.person_for(first["access_token"]) is None and world.person_for(second["access_token"]) is None


def test_unknown_scopes_are_dropped_not_refused(world):
    client_id = world.register()["client_id"]
    verifier = secrets.token_urlsafe(48)
    decided = world.consent(world.authorize(client_id, verifier=verifier, scope="openid profile"))
    code = parse_qs(urlsplit(decided.json()["redirect"]).query)["code"][0]
    assert world.exchange(client_id, code, verifier).json()["scope"] == "mcp"


def test_tokens_and_codes_rest_only_as_hashes(world):
    client_id, tokens = world.connect()
    dump = "\n".join(sqlite3.connect(world.db).iterdump())
    assert tokens["access_token"] not in dump and tokens["refresh_token"] not in dump
    assert mcp_oauth.ACCESS_TOKEN_PREFIX not in dump


def test_access_token_expires(world):
    _, tokens = world.connect()
    world.now += 3601
    assert world.person_for(tokens["access_token"]) is None


# ---------------------------------------------------------------------- PKCE


def test_missing_pkce_is_an_error_sent_back_to_the_client_with_iss(world):
    client_id = world.register()["client_id"]
    resp = world.authorize(client_id, code_challenge=None, code_challenge_method=None)
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert resp.headers["location"].startswith(CALLBACK)
    assert query["error"] == ["invalid_request"] and query["iss"] == [BASE] and query["state"] == ["st-1"]


def test_plain_pkce_is_refused(world):
    client_id = world.register()["client_id"]
    resp = world.authorize(client_id, code_challenge="a" * 43, code_challenge_method="plain")
    assert parse_qs(urlsplit(resp.headers["location"]).query)["error"] == ["invalid_request"]


def test_wrong_verifier_gets_invalid_grant(world):
    client_id = world.register()["client_id"]
    code = world.code_for(client_id, secrets.token_urlsafe(48))
    resp = world.exchange(client_id, code, secrets.token_urlsafe(48))
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"


# --------------------------------------------------- redirects (open redirect)


def test_unknown_client_gets_a_local_error_page_not_a_redirect(world):
    resp = world.authorize("azm_client_nope")
    assert resp.status_code == 400 and "location" not in resp.headers
    assert resp.headers["x-frame-options"] == "DENY"


def test_unregistered_redirect_uri_is_never_followed(world):
    client_id = world.register()["client_id"]
    resp = world.authorize(client_id, redirect_uri="https://evil.example/steal")
    assert resp.status_code == 400 and "location" not in resp.headers


def test_loopback_redirect_may_use_any_port(world):
    client_id = world.register(redirect_uris=["http://127.0.0.1/callback"])["client_id"]
    ok = world.authorize(client_id, redirect_uri="http://127.0.0.1:53682/callback")
    assert ok.status_code == 302 and ok.headers["location"].startswith("/authorize/")
    bad = world.authorize(client_id, redirect_uri="http://127.0.0.1:53682/other")
    assert bad.status_code == 400


# ------------------------------------------------------------ resource (8707)


def test_resource_for_another_server_is_invalid_target(world):
    client_id = world.register()["client_id"]
    resp = world.authorize(client_id, resource="https://other.test/mcp")
    assert parse_qs(urlsplit(resp.headers["location"]).query)["error"] == ["invalid_target"]


def test_token_request_cannot_switch_resource(world):
    client_id = world.register()["client_id"]
    verifier = secrets.token_urlsafe(48)
    code = world.code_for(client_id, verifier)
    resp = world.exchange(client_id, code, verifier, resource="https://other.test/mcp")
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_target"


# ------------------------------------------------------- replay and rotation


def test_code_reuse_fails_and_ends_the_connection(world):
    client_id = world.register()["client_id"]
    verifier = secrets.token_urlsafe(48)
    code = world.code_for(client_id, verifier)
    first = world.exchange(client_id, code, verifier).json()
    again = world.exchange(client_id, code, verifier)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    assert world.person_for(first["access_token"]) is None


def test_refresh_rotates_and_reuse_ends_the_connection(world):
    client_id, tokens = world.connect()
    rotated = world.refresh(client_id, tokens["refresh_token"])
    assert rotated.status_code == 200, rotated.text
    new = rotated.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    assert world.person_for(new["access_token"]) == "usr_a"
    replay = world.refresh(client_id, tokens["refresh_token"])
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert world.person_for(new["access_token"]) is None
    assert world.refresh(client_id, new["refresh_token"]).status_code == 400


def test_refresh_by_another_client_is_refused(world):
    client_id, tokens = world.connect()
    other = world.register()["client_id"]
    assert world.refresh(other, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert world.refresh(client_id, tokens["refresh_token"]).status_code == 200


def test_refresh_cannot_widen_scope(world):
    client_id, tokens = world.connect()
    assert world.refresh(client_id, tokens["refresh_token"], scope="mcp admin").json()["error"] == "invalid_scope"


def test_refresh_stops_when_the_person_loses_access(world):
    client_id, tokens = world.connect()
    world.blocked.add("usr_a")
    assert world.refresh(client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_parallel_code_redemption_has_one_winner(world):
    client_id = world.register()["client_id"]
    verifier = secrets.token_urlsafe(48)
    code = world.code_for(client_id, verifier)
    record = world.store.code(code)
    assert world.store.spend_code(record.code_hash) is True
    assert world.store.spend_code(record.code_hash) is False


# ------------------------------------------------------------------- consent


def test_deny_returns_access_denied_and_creates_no_connection(world):
    client_id = world.register()["client_id"]
    decided = world.consent(world.authorize(client_id), decision="deny")
    query = parse_qs(urlsplit(decided.json()["redirect"]).query)
    assert query["error"] == ["access_denied"] and query["state"] == ["st-1"] and query["iss"] == [BASE]
    assert world.store.grants_for_person("usr_a") == []


def test_consent_needs_the_browser_that_started_it(world):
    client_id = world.register()["client_id"]
    started = world.authorize(client_id)
    stranger = TestClient(world.client.app, base_url=BASE, follow_redirects=False)
    assert world.consent(started, browser=stranger).status_code == 410
    request_id = parse_qs(urlsplit(started.headers["location"]).query)["request"][0]
    assert stranger.get(f"/me/api/mcp/requests/{request_id}", headers={"x-test-person": "usr_a"}).status_code == 410
    assert world.consent(started).status_code == 200  # the starting browser still can


def test_consent_needs_sign_in_and_csrf(world):
    client_id = world.register()["client_id"]
    started = world.authorize(client_id)
    request_id = parse_qs(urlsplit(started.headers["location"]).query)["request"][0]
    url = f"/me/api/mcp/requests/{request_id}"
    assert world.client.post(url, json={"decision": "allow"}).status_code == 401
    assert world.client.post(url, json={"decision": "allow"}, headers={"x-test-person": "usr_a"}).status_code == 403
    assert world.consent(started).status_code == 200


def test_consent_is_answered_once(world):
    client_id = world.register()["client_id"]
    started = world.authorize(client_id)
    assert world.consent(started).status_code == 200
    assert world.consent(started).status_code == 410


def test_consent_request_expires(world):
    client_id = world.register()["client_id"]
    started = world.authorize(client_id)
    world.now += 601
    assert world.consent(started).status_code == 410


def test_person_without_access_cannot_approve_and_the_request_survives(world):
    client_id = world.register()["client_id"]
    started = world.authorize(client_id)
    world.blocked.add("usr_a")
    refused = world.consent(started)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "no_access"
    world.blocked.clear()
    assert world.consent(started).status_code == 200


def test_reapproval_is_a_new_connection_and_never_revives_a_revoked_one(world):
    client_id, first = world.connect()
    grant = world.store.grants_for_person("usr_a")[0]
    world.store.revoke_grant(grant.id, "test")
    _, second = world.connect(client_id)
    assert world.person_for(first["access_token"]) is None
    assert world.person_for(second["access_token"]) == "usr_a"


# ---------------------------------------------------------------- revocation


def test_client_revocation_ends_the_connection(world):
    client_id, tokens = world.connect()
    resp = world.client.post("/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": client_id})
    assert resp.status_code == 200
    assert world.person_for(tokens["access_token"]) is None
    assert world.client.post("/oauth/revoke", data={"token": "unknown", "client_id": client_id}).status_code == 200


def test_person_sees_and_revokes_only_their_own_connections(world):
    _, ann = world.connect(person="usr_a")
    _, bo = world.connect(person="usr_b")
    listed = world.client.get("/me/api/mcp/connections", headers={"x-test-person": "usr_a"}).json()["connections"]
    assert [c["client_name"] for c in listed] == ["Test App"]
    bo_grant = world.store.grants_for_person("usr_b")[0].id
    headers = {"x-test-person": "usr_a", "x-test-csrf": "ok"}
    assert world.client.post(f"/me/api/mcp/connections/{bo_grant}/revoke", headers=headers).status_code == 404
    assert world.person_for(bo["access_token"]) == "usr_b"
    assert world.client.post(f"/me/api/mcp/connections/{listed[0]['id']}/revoke",
                             headers={"x-test-person": "usr_a"}).status_code == 403
    assert world.client.post(f"/me/api/mcp/connections/{listed[0]['id']}/revoke", headers=headers).status_code == 200
    assert world.person_for(ann["access_token"]) is None
    assert world.client.get("/me/api/mcp/connections", headers={"x-test-person": "usr_a"}).json()["connections"] == []


# ------------------------------------------------- client metadata documents


def _document(**overrides) -> dict:
    return {"client_id": CIMD_ID, "client_name": "Doc App", "redirect_uris": [CALLBACK],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", **overrides}


def test_metadata_document_client_connects_and_is_cached(world):
    world.documents[CIMD_ID] = _document()
    client_id, tokens = world.connect(CIMD_ID)
    assert world.person_for(tokens["access_token"]) == "usr_a"
    assert world.store.client(CIMD_ID).kind == "cimd"
    fetched = len(world.fetches)
    world.connect(CIMD_ID)
    assert len(world.fetches) == fetched  # inside max-age


def test_metadata_document_names_its_domain_on_the_consent_page(world):
    world.documents[CIMD_ID] = _document()
    started = world.authorize(CIMD_ID)
    request_id = parse_qs(urlsplit(started.headers["location"]).query)["request"][0]
    client = world.client.get(f"/me/api/mcp/requests/{request_id}", headers={"x-test-person": "usr_a"}).json()["client"]
    assert client["verified_domain"] == "client.example" and client["registration"] == "metadata_document"


def test_a_document_that_also_offers_none_is_accepted(world):
    # ChatGPT's published document prefers private_key_jwt but lists none.
    world.documents[CIMD_ID] = _document(token_endpoint_auth_method="private_key_jwt",
                                         token_endpoint_auth_methods_supported=["none", "private_key_jwt"])
    world.connect(CIMD_ID)


@pytest.mark.parametrize("document", [
    _document(client_id="https://elsewhere.example/client.json"),
    _document(redirect_uris=["http://evil.example/cb"]),
    _document(token_endpoint_auth_method="private_key_jwt"),
    _document(client_name=""),
    b"not json",
])
def test_bad_metadata_documents_are_refused_locally(world, document):
    world.documents[CIMD_ID] = document
    resp = world.authorize(CIMD_ID)
    assert resp.status_code == 400 and "location" not in resp.headers


def test_unreachable_document_is_refused_but_a_recent_copy_carries_an_outage(world):
    assert world.authorize(CIMD_ID).status_code == 400
    world.documents[CIMD_ID] = _document()
    world.connect(CIMD_ID)
    del world.documents[CIMD_ID]
    world.now += 700  # past max-age, inside the outage allowance
    assert world.authorize(CIMD_ID).status_code == 302


@pytest.mark.parametrize("client_id", ["http://client.example/c.json", "https://client.example",
                                       "https://client.example/", "https://u@client.example/c.json"])
def test_only_https_urls_with_a_path_are_document_ids(client_id):
    assert not mcp_oauth.is_client_document_url(client_id)

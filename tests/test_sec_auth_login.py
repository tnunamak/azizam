"""Browser authentication regressions using test-owned state and fake IdP I/O."""
import asyncio
from contextlib import contextmanager
import base64
import ipaddress
import json
import sqlite3
import time
from dataclasses import replace

import pytest
from authlib.jose import JsonWebKey, jwt
from fastapi.testclient import TestClient

import proxy
from gateway import accounts, admin_store
from gateway.admin_oidc import AdminSession, decode_session_cookie, csrf_token_for_session, encode_session_cookie, session_from_claims
from gateway.oidc_jwks import JWKSCache
from test_exposure_oidc_security import oidc_gateway, _client, _start_login
from tests.support.signin import seed_rules


@contextmanager
def browser():
    # Start no background engine tasks: this journey exercises HTTP auth only.
    client = _client()
    try:
        yield client
    finally:
        client.close()


def login_claims(monkeypatch, client, *, subject, email, groups=()):
    state, record = _start_login(client)

    async def exchange(code, *, config, pkce):
        return {"id_token": "fake-token"}

    async def verify(token, *, config):
        return {"iss": config.issuer, "aud": config.client_id, "sub": subject,
                "email": email, "email_verified": True, "groups": list(groups),
                "nonce": record.nonce, "exp": 4_000_000_000, "iat": 1_700_000_000}

    monkeypatch.setattr(proxy, "_oidc_exchange_code", exchange)
    monkeypatch.setattr(proxy, "_oidc_verify_id_token", verify)
    response = client.get("/admin/oidc/callback", params={"state": state, "code": "test"}, follow_redirects=False)
    assert response.status_code == 302, response.text
    return admin_store.upsert_oidc_user(proxy.ADMIN_OIDC_CONFIG.issuer, subject, email=email, path=proxy.ADMIN_STORE.path)


def test_sec06_same_email_new_subject_or_issuer_cannot_inherit_owner(oidc_gateway, monkeypatch):
    existing = admin_store.upsert_oidc_user(proxy.ADMIN_OIDC_CONFIG.issuer, "original", email="owner@example.test", path=oidc_gateway)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=existing["id"], path=oidc_gateway)
    with browser() as client:
        attacker = login_claims(monkeypatch, client, subject="different", email="owner@example.test")
        assert not admin_store.list_admin_roles(subject_id=attacker["id"], path=oidc_gateway)
        assert client.get("/admin/api/session").status_code == 403
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", replace(proxy.ADMIN_OIDC_CONFIG, issuer="https://different-idp.example"))
    with browser() as client:
        attacker = login_claims(monkeypatch, client, subject="original", email="owner@example.test")
        assert not admin_store.list_admin_roles(subject_id=attacker["id"], path=oidc_gateway)


def test_sec08_legacy_owner_survives_real_oidc_callback_after_migration(oidc_gateway, monkeypatch):
    user = admin_store.upsert_oidc_user(proxy.ADMIN_OIDC_CONFIG.issuer, "old-owner", email="owner@example.test", path=oidc_gateway)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=user["id"], granted_by="oidc", path=oidc_gateway)
    # Reconstruct the old assignment table to exercise the real idempotent migration.
    with sqlite3.connect(oidc_gateway) as conn:
        conn.execute("ALTER TABLE admin_role_assignments DROP COLUMN provenance")
    admin_store.init_db(oidc_gateway)
    admin_store.init_db(oidc_gateway)
    with browser() as client:
        returned = login_claims(monkeypatch, client, subject="old-owner", email="changed@example.test")
        assert returned["id"] == user["id"]
        response = client.get("/admin/api/session")
        assert response.status_code == 200, response.text
        assert "owner" in response.json()["actor"]["roles"]
    grants = admin_store.list_admin_roles(subject_id=user["id"], path=oidc_gateway)
    assert [(r["role"], r["provenance"]) for r in grants] == [("owner", "manual")]


def test_sec08_group_removal_revokes_oidc_grant_on_next_login(oidc_gateway, monkeypatch):
    seed_rules(oidc_gateway, domains=["example.test"], groups=["admins"])
    with browser() as client:
        user = login_claims(monkeypatch, client, subject="admin-sub", email="admin@example.test", groups=["/admins"])
        assert client.get("/admin/api/session").status_code == 200
        grants = admin_store.list_admin_roles(subject_id=user["id"], path=oidc_gateway)
        assert [r["role"] for r in grants] == ["admin"]
        login_claims(monkeypatch, client, subject="admin-sub", email="admin@example.test", groups=[])
        assert client.get("/admin/api/session").status_code == 403
        assert not admin_store.list_admin_roles(subject_id=user["id"], path=oidc_gateway)
        removed = admin_store.list_admin_roles(subject_id=user["id"], include_revoked=True, path=oidc_gateway)
        assert [(r["role"], r["provenance"]) for r in removed] == [("admin", "oidc")]


def test_sec06_revoking_all_grants_does_not_reopen_email_bootstrap(oidc_gateway, monkeypatch):
    original = admin_store.upsert_oidc_user(proxy.ADMIN_OIDC_CONFIG.issuer, "original", path=oidc_gateway)
    grant = admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=original["id"], path=oidc_gateway)
    admin_store.revoke_admin_role(grant["id"], path=oidc_gateway)
    with browser() as client:
        attacker = login_claims(monkeypatch, client, subject="replacement", email="owner@example.test")
        assert not admin_store.list_admin_roles(subject_id=attacker["id"], path=oidc_gateway)


def test_sec10_sidless_cookie_revocation_is_session_specific(oidc_gateway):
    accounts.initialize(oidc_gateway)
    cfg = proxy.ADMIN_OIDC_CONFIG
    now = time.time()
    first = AdminSession("old-user-one", "one@example.test", "none", now, now + 600)
    second = AdminSession("old-user-two", "two@example.test", "none", now, now + 600)
    decoded = [decode_session_cookie(encode_session_cookie(session, signing_key=cfg.signing_key), signing_key=cfg.signing_key)
               for session in (first, second)]
    assert decoded[0].session_id and decoded[0].session_id != decoded[1].session_id
    accounts.revoke_session(decoded[0].session_id, int(decoded[0].expires_at), path=oidc_gateway)
    assert not accounts.session_allowed(decoded[0].subject, decoded[0].session_id, path=oidc_gateway)
    assert accounts.session_allowed(decoded[1].subject, decoded[1].session_id, path=oidc_gateway)


@pytest.mark.parametrize("registered,access_row", [(False, False), (False, True), (True, True)])
def test_sec10_post_logout_revokes_replayed_legacy_and_registered_cookies(oidc_gateway, registered, access_row):
    accounts.initialize(oidc_gateway)
    cfg = proxy.ADMIN_OIDC_CONFIG
    user = admin_store.upsert_oidc_user(cfg.issuer, "logout-owner", path=oidc_gateway)
    admin_store.assign_admin_role(role="owner", subject_kind="user", subject_id=user["id"], path=oidc_gateway)
    if not access_row:
        # Every person has an access row; the cookie must still be revoked without one.
        with accounts.connection(oidc_gateway) as conn:
            conn.execute("DELETE FROM account_access WHERE user_id = ?", (user["id"],))
    session = session_from_claims({"sub": user["id"], "email": ""}, role="owner", cookie=cfg.cookie)
    if registered:
        accounts.register_session(user["id"], session.session_id, int(session.expires_at), path=oidc_gateway)
    token = encode_session_cookie(session, signing_key=cfg.signing_key)
    with browser() as client:
        client.cookies.set(cfg.cookie.name, token)
        assert client.post("/logout").status_code == 403
        assert accounts.session_allowed(user["id"], session.session_id, path=oidc_gateway)
        response = client.post("/logout", headers={"x-azizam-csrf": csrf_token_for_session(session, signing_key=cfg.signing_key)}, follow_redirects=False)
        assert response.status_code == 303
        client.cookies.set(cfg.cookie.name, token)
        assert client.get("/admin/api/session").status_code == 401
        assert not accounts.session_allowed(user["id"], session.session_id, path=oidc_gateway)
        # Public authentication routes also refuse replayed cookies as identity.
        client.cookies.set(cfg.cookie.name, token)
        assert client.get("/auth/api/options").json().get("signed_in_oidc") is None


def test_sec11_jwks_refreshes_on_ttl_and_unknown_kid_and_limits_refresh(monkeypatch):
    clock = [0.0]
    cache = JWKSCache(ttl_seconds=300, refresh_interval=30, clock=lambda: clock[0])
    old = JsonWebKey.generate_key("RSA", 2048, {"kid": "old"}, is_private=True)
    new = JsonWebKey.generate_key("RSA", 2048, {"kid": "new"}, is_private=True)
    current = [{"keys": [old.as_dict(is_private=False)]}]
    calls = []

    async def fetch(uri):
        calls.append(uri)
        return current[0]

    monkeypatch.setattr(cache, "_fetch", fetch)
    def token(key, kid):
        # A JWK carries its own kid and Authlib overwrites the supplied header;
        # PEM keeps the adversarial header independent of the signing key.
        encoded = jwt.encode({"alg": "RS256", "kid": kid}, {"sub": "person", "exp": int(time.time()) + 600}, key.as_pem(is_private=True)).decode()
        header = encoded.split(".")[0]
        assert json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))["kid"] == kid
        return encoded
    async def exercise():
        assert (await cache.verify(token(old, "old"), uri="fake-jwks", leeway=0))["sub"] == "person"
        current[0] = {"keys": [new.as_dict(is_private=False)]}
        assert (await cache.verify(token(new, "new"), uri="fake-jwks", leeway=0))["sub"] == "person"
        assert len(calls) == 2
        for kid in ("absent1", "absent2"):
            with pytest.raises(ValueError, match="Invalid identity token"):
                await cache.verify(token(new, kid), uri="fake-jwks", leeway=0)
        assert len(calls) == 2
        clock[0] = 301
        assert (await cache.verify(token(new, "new"), uri="fake-jwks", leeway=0))["sub"] == "person"
        assert len(calls) == 3
        with pytest.raises(ValueError, match="Invalid identity token"):
            await cache.verify(token(old, "new"), uri="fake-jwks", leeway=0)
    asyncio.run(exercise())


def test_sec11_malformed_tokens_fail_generically():
    cache = JWKSCache()
    for token in ("!", base64.urlsafe_b64encode(json.dumps([]).encode()).decode() + ".x.y"):
        with pytest.raises(ValueError, match="Invalid identity token"):
            asyncio.run(cache.verify(token, uri="never-fetched", leeway=0))


def test_sec11_gateway_verifier_refreshes_keys_when_provider_rotates(monkeypatch):
    import httpx
    from test_exposure_oidc_security import _oidc_config
    cfg = _oidc_config()
    old = JsonWebKey.generate_key("RSA", 2048, {"kid": "old"}, is_private=True)
    new = JsonWebKey.generate_key("RSA", 2048, {"kid": "new"}, is_private=True)
    served = [{"keys": [old.as_dict(is_private=False)]}]
    calls = []
    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, uri):
            calls.append(uri)
            return httpx.Response(200, json=served[0], request=httpx.Request("GET", uri))
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    proxy.ADMIN_OIDC_JWKS_CACHE.clear()
    async def exercise():
        for key in (old, new):
            encoded = jwt.encode({"alg": "RS256", "kid": key.kid}, {"sub": "person", "exp": int(time.time()) + 600}, key).decode()
            served[0] = {"keys": [key.as_dict(is_private=False)]}
            assert (await proxy._oidc_verify_id_token(encoded, config=cfg))["sub"] == "person"
        assert calls == [cfg.jwks_uri, cfg.jwks_uri]
    try:
        asyncio.run(exercise())
    finally:
        proxy.ADMIN_OIDC_JWKS_CACHE.clear()


def test_sec12_login_and_setup_use_verified_forwarded_client_and_fail_closed(oidc_gateway, monkeypatch):
    accounts.initialize(oidc_gateway)
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [ipaddress.ip_network("127.0.0.1/32")])
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", False)
    seen = []
    def fake_login(email, password, ip, *, path):
        seen.append(ip)
        raise accounts.AccountError("invalid_login", "Invalid login", 401)
    monkeypatch.setattr(accounts, "login", fake_login)
    monkeypatch.setattr(accounts, "throttle", lambda ip, email, **kw: seen.append(ip))
    with browser() as client:
        for ip in ("203.0.113.1", "203.0.113.2"):
            response = client.post("/auth/api/login", headers={"x-forwarded-for": ip}, json={"email": "user@example.test", "password": "wrong"})
            assert response.status_code == 401
            response = client.post("/setup/local-login", headers={"x-forwarded-for": ip}, data={"email": "user@example.test", "password": "wrong"}, follow_redirects=False)
            assert response.status_code == 401
        assert seen == ["203.0.113.1", "203.0.113.1", "203.0.113.2", "203.0.113.2"]
        for route, body in (("/auth/api/login", {"json": {"email": "x", "password": "x"}}),
                            ("/setup/local-login", {"data": {"email": "x@example.test", "password": "x"}})):
            assert client.post(route, headers={"x-forwarded-for": "malformed,,chain"}, **body).status_code == 400
        assert len(seen) == 4


def test_sec13_login_flow_creation_is_rate_limited_by_verified_client(oidc_gateway, monkeypatch):
    accounts.initialize(oidc_gateway)
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [ipaddress.ip_network("127.0.0.1/32")])
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", False)
    with browser() as client:
        for _ in range(20):
            assert client.get("/login", headers={"x-forwarded-for": "203.0.113.1"}, follow_redirects=False).status_code == 302
        response = client.get("/login", headers={"x-forwarded-for": "203.0.113.1"}, follow_redirects=False)
        assert response.status_code == 429
        assert len(proxy.ADMIN_OIDC_STATES) == 20
        assert client.get("/login", headers={"x-forwarded-for": "203.0.113.2"}, follow_redirects=False).status_code == 302
        assert client.get("/login", headers={"x-forwarded-for": "bad"}, follow_redirects=False).status_code == 400

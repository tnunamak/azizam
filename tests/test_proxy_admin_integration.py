from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import admin_store  # noqa: E402
from gateway.admin_oidc import (  # noqa: E402
    CookieConfig,
    OIDCConfig,
    csrf_token_for_session,
    encode_session_cookie,
    session_from_claims,
)
from gateway.admin_runtime import create_admin_service_account_key  # noqa: E402


class ProxyAdminIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        self.old_path = proxy.ADMIN_STORE.path
        self.old_oidc = proxy.ADMIN_OIDC_CONFIG
        self.old_signing_key = proxy.ADMIN_AUTH.signing_key
        self.old_cookie = proxy.ADMIN_AUTH.cookie
        self.old_auth_enabled = proxy.AUTH_ENABLED
        self.old_auth_registry = proxy.AUTH_REGISTRY
        self.old_refresh_auth_registry = proxy.refresh_auth_registry
        proxy.ADMIN_STORE.path = self.db
        proxy.ADMIN_OIDC_STATES.clear()
        proxy.AUTH_ENABLED = True
        proxy.AUTH_REGISTRY = {}
        proxy.refresh_auth_registry = lambda force=False: None
        admin_store.init_db(self.db)
        self.admin_key, _, _ = create_admin_service_account_key(
            "test-owner", path=self.db
        )
        self.client = TestClient(proxy.app)

    def tearDown(self) -> None:
        proxy.ADMIN_STORE.path = self.old_path
        proxy.ADMIN_OIDC_CONFIG = self.old_oidc
        proxy.ADMIN_AUTH.signing_key = self.old_signing_key
        proxy.ADMIN_AUTH.cookie = self.old_cookie
        proxy.AUTH_ENABLED = self.old_auth_enabled
        proxy.AUTH_REGISTRY = self.old_auth_registry
        proxy.refresh_auth_registry = self.old_refresh_auth_registry
        proxy.ADMIN_OIDC_STATES.clear()
        self.tmp.cleanup()

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_key}"}

    def test_admin_authenticator_uses_configured_oidc_cookie_object(self) -> None:
        self.assertIs(proxy.ADMIN_AUTH.cookie, proxy.ADMIN_OIDC_CONFIG.cookie)

    def test_admin_api_is_mounted_and_separates_app_keys(self) -> None:
        overview = self.client.get("/admin/api/overview", headers=self._auth())
        self.assertEqual(overview.status_code, 200, overview.text)

        created = self.client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers=self._auth(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        app_id = created.json()["app"]["id"]

        key_response = self.client.post(
            f"/admin/api/apps/{app_id}/keys",
            json={"label": "Brother"},
            headers=self._auth(),
        )
        self.assertEqual(key_response.status_code, 201, key_response.text)
        app_key = key_response.json()["credential"]["api_key"]
        self.assertTrue(app_key.startswith("opk_"))

        forbidden = self.client.get(
            "/admin/api/overview",
            headers={"Authorization": f"Bearer {app_key}"},
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["error"]["code"], "admin_forbidden")

    def test_admin_created_app_key_authenticates_runtime_with_profile(self) -> None:
        profile = self.client.post(
            "/admin/api/profiles",
            json={
                "name": "chat-only",
                "profile": {
                    "id": "chat-only",
                    "protocols": ["openai.chat"],
                    "fallback": {"cloud_allowed": False},
                    "status_visibility": {"level": "basic"},
                },
            },
            headers=self._auth(),
        ).json()["profile"]
        created = self.client.post(
            "/admin/api/apps",
            json={"name": "brother-chat", "profile_id": profile["id"]},
            headers=self._auth(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        app_id = created.json()["app"]["id"]
        key_response = self.client.post(
            f"/admin/api/apps/{app_id}/keys",
            json={"label": "Brother"},
            headers=self._auth(),
        )
        self.assertEqual(key_response.status_code, 201, key_response.text)
        app_key = key_response.json()["credential"]["api_key"]

        capabilities = self.client.get(
            "/gateway/v1/capabilities",
            headers={"Authorization": f"Bearer {app_key}"},
        )
        self.assertEqual(capabilities.status_code, 200, capabilities.text)

        denied = self.client.post(
            "/v1/images/generations",
            json={"model": "flux2-klein-4b", "prompt": "test"},
            headers={"Authorization": f"Bearer {app_key}"},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "policy_denied")

        admin_key_on_runtime = self.client.get(
            "/gateway/v1/capabilities",
            headers={"Authorization": f"Bearer {self.admin_key}"},
        )
        self.assertEqual(admin_key_on_runtime.status_code, 401)

    def test_admin_dashboard_renders_with_admin_key(self) -> None:
        response = self.client.get("/admin", headers=self._auth())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("AI Gateway Admin", response.text)
        self.assertIn("Create App", response.text)
        self.assertIn("No Apps yet", response.text)
        self.assertNotIn("Mint runtime key", response.text)

    def test_admin_dashboard_requires_admin_auth(self) -> None:
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "admin_unauthenticated")

    def test_admin_dashboard_redirects_browser_to_login_when_oidc_enabled(self) -> None:
        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset({"example.com"}),
            admin_emails=frozenset({"owner@example.com"}),
            signing_key=b"s" * 32,
            cookie=CookieConfig(secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie

        response = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(response.headers["location"], "/login?return_to=%2Fadmin")

    def test_oidc_login_callback_creates_signed_admin_session(self) -> None:
        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset({"example.com"}),
            admin_emails=frozenset({"owner@example.com"}),
            signing_key=b"s" * 32,
            cookie=CookieConfig(secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie

        login = self.client.get(
            "/login?return_to=/admin", follow_redirects=False
        )
        self.assertEqual(login.status_code, 302, login.text)
        self.assertIn("https://idp.example/authorize", login.headers["location"])

        state, record = next(iter(proxy.ADMIN_OIDC_STATES.items()))

        async def fake_exchange(_code, *, config, pkce):
            self.assertEqual(pkce.verifier, record.pkce_verifier)
            return {"id_token": "fake.id.token"}

        async def fake_verify(_id_token, *, config):
            return {
                "iss": cfg.issuer,
                "aud": cfg.client_id,
                "sub": "oidc-subject",
                "email": "owner@example.com",
                "email_verified": True,
                "nonce": record.nonce,
                "exp": 4_000_000_000,
                "iat": 1_700_000_000,
            }

        with patch.object(proxy, "_oidc_exchange_code", fake_exchange), patch.object(
            proxy, "_oidc_verify_id_token", fake_verify
        ):
            callback = self.client.get(
                f"/admin/oidc/callback?state={state}&code=abc",
                follow_redirects=False,
            )
        self.assertEqual(callback.status_code, 302, callback.text)
        self.assertIn(cfg.cookie.name, callback.headers.get("set-cookie", ""))

        dashboard = self.client.get("/admin")
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertIn("owner@example.com", dashboard.text)

    def test_oidc_login_uses_configured_cookie_name(self) -> None:
        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset({"example.com"}),
            admin_emails=frozenset({"owner@example.com"}),
            signing_key=b"s" * 32,
            cookie=CookieConfig(name="custom_admin_session", secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie

        login = self.client.get("/login?return_to=/admin", follow_redirects=False)
        self.assertEqual(login.status_code, 302, login.text)
        state, record = next(iter(proxy.ADMIN_OIDC_STATES.items()))

        async def fake_exchange(_code, *, config, pkce):
            return {"id_token": "fake.id.token"}

        async def fake_verify(_id_token, *, config):
            return {
                "iss": cfg.issuer,
                "aud": cfg.client_id,
                "sub": "oidc-subject",
                "email": "owner@example.com",
                "email_verified": True,
                "nonce": record.nonce,
                "exp": 4_000_000_000,
                "iat": 1_700_000_000,
            }

        with patch.object(proxy, "_oidc_exchange_code", fake_exchange), patch.object(
            proxy, "_oidc_verify_id_token", fake_verify
        ):
            callback = self.client.get(
                f"/admin/oidc/callback?state={state}&code=abc",
                follow_redirects=False,
            )
        self.assertEqual(callback.status_code, 302, callback.text)
        self.assertIn("custom_admin_session=", callback.headers.get("set-cookie", ""))
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_oidc_return_to_is_limited_to_safe_browser_paths(self) -> None:
        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset({"example.com"}),
            admin_emails=frozenset({"owner@example.com"}),
            signing_key=b"s" * 32,
            cookie=CookieConfig(secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie

        login = self.client.get(
            "/login?return_to=https://evil.example/steal",
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 302, login.text)
        state, record = next(iter(proxy.ADMIN_OIDC_STATES.items()))
        self.assertEqual(record.return_to, "/")

        async def fake_exchange(_code, *, config, pkce):
            return {"id_token": "fake.id.token"}

        async def fake_verify(_id_token, *, config):
            return {
                "iss": cfg.issuer,
                "aud": cfg.client_id,
                "sub": "oidc-subject",
                "email": "owner@example.com",
                "email_verified": True,
                "nonce": record.nonce,
                "exp": 4_000_000_000,
                "iat": 1_700_000_000,
            }

        with patch.object(proxy, "_oidc_exchange_code", fake_exchange), patch.object(
            proxy, "_oidc_verify_id_token", fake_verify
        ):
            callback = self.client.get(
                f"/admin/oidc/callback?state={state}&code=abc",
                follow_redirects=False,
            )
        self.assertEqual(callback.status_code, 302, callback.text)
        self.assertEqual(callback.headers["location"], "/")

    def test_legacy_login_routes_redirect_to_single_login_entrypoint(self) -> None:
        self.assertEqual(
            self.client.get("/me/login", follow_redirects=False).headers["location"],
            "/login?return_to=%2F",
        )
        self.assertEqual(
            self.client.get(
                "/admin/login?return_to=/admin",
                follow_redirects=False,
            ).headers["location"],
            "/login?return_to=%2Fadmin",
        )

    def test_oidc_return_to_allows_user_portal(self) -> None:
        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset({"example.com"}),
            admin_emails=frozenset({"owner@example.com"}),
            signing_key=b"s" * 32,
            cookie=CookieConfig(secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie

        login = self.client.get(
            "/login?return_to=/me", follow_redirects=False
        )
        self.assertEqual(login.status_code, 302, login.text)
        state, record = next(iter(proxy.ADMIN_OIDC_STATES.items()))
        self.assertEqual(record.return_to, "/")

        async def fake_exchange(_code, *, config, pkce):
            return {"id_token": "fake.id.token"}

        async def fake_verify(_id_token, *, config):
            return {
                "iss": cfg.issuer,
                "aud": cfg.client_id,
                "sub": "brother-subject",
                "email": "brother@example.com",
                "email_verified": True,
                "nonce": record.nonce,
                "exp": 4_000_000_000,
                "iat": 1_700_000_000,
            }

        with patch.object(proxy, "_oidc_exchange_code", fake_exchange), patch.object(
            proxy, "_oidc_verify_id_token", fake_verify
        ):
            callback = self.client.get(
                f"/admin/oidc/callback?state={state}&code=abc",
                follow_redirects=False,
            )
        self.assertEqual(callback.status_code, 302, callback.text)
        self.assertEqual(callback.headers["location"], "/")
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(
            self.client.get("/me/login", follow_redirects=False).headers["location"],
            "/login?return_to=%2F",
        )

    def test_user_portal_shows_oidc_owned_apps_and_self_manages_keys(self) -> None:
        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset({"example.com"}),
            admin_emails=frozenset({"owner@example.com"}),
            signing_key=b"s" * 32,
            cookie=CookieConfig(secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie

        user = admin_store.upsert_oidc_user(
            cfg.issuer,
            "brother-subject",
            email="brother@example.com",
            display_name="Brother",
            path=self.db,
        )
        profile = self.client.post(
            "/admin/api/profiles",
            json={
                "name": "brother-chat",
                "profile": {
                    "id": "brother-chat",
                    "protocols": ["openai.chat"],
                },
            },
            headers=self._auth(),
        ).json()["profile"]
        created = self.client.post(
            "/admin/api/apps",
            json={
                "name": "brother-laptop",
                "kind": "personal",
                "owner_user_id": user["id"],
                "profile_id": profile["id"],
            },
            headers=self._auth(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        app_id = created.json()["app"]["id"]

        session = session_from_claims(
            {"sub": user["id"], "email": "brother@example.com"},
            role="none",
            cookie=cfg.cookie,
        )
        self.client.cookies.set(
            cfg.cookie.name,
            encode_session_cookie(session, signing_key=cfg.signing_key),
        )
        csrf = csrf_token_for_session(session, signing_key=cfg.signing_key)

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("brother@example.com", page.text)
        self.assertIn("brother-laptop", page.text)

        session_resp = self.client.get("/me/api/session")
        self.assertEqual(session_resp.status_code, 200, session_resp.text)
        self.assertEqual(session_resp.json()["apps"][0]["id"], app_id)
        self.assertNotIn("hash", repr(session_resp.json()))

        missing_header = self.client.post(f"/me/api/apps/{app_id}/keys", json={})
        self.assertEqual(missing_header.status_code, 403)

        minted = self.client.post(
            f"/me/api/apps/{app_id}/keys",
            json={"label": "Brother laptop"},
            headers={
                "x-ai-gateway-user-action": "1",
                "x-ai-gateway-csrf": csrf,
            },
        )
        self.assertEqual(minted.status_code, 201, minted.text)
        credential = minted.json()["credential"]
        self.assertTrue(credential["api_key"].startswith("opk_"))
        self.assertNotIn("hash", credential)

        revoked = self.client.post(
            f"/me/api/credentials/{credential['id']}/revoke",
            json={},
            headers={
                "x-ai-gateway-user-action": "1",
                "x-ai-gateway-csrf": csrf,
            },
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertFalse(revoked.json()["credential"]["active"])

        logout = self.client.get("/me/logout", follow_redirects=False)
        self.assertEqual(logout.status_code, 302)
        self.assertEqual(logout.headers["location"], "/")

    def test_user_portal_access_request_creates_pending_personal_app(self) -> None:
        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset({"example.com"}),
            admin_emails=frozenset({"owner@example.com"}),
            signing_key=b"s" * 32,
            cookie=CookieConfig(secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie

        user = admin_store.upsert_oidc_user(
            cfg.issuer,
            "new-subject",
            email="new@example.com",
            display_name="New",
            path=self.db,
        )
        session = session_from_claims(
            {"sub": user["id"], "email": "new@example.com"},
            role="none",
            cookie=cfg.cookie,
        )
        self.client.cookies.set(
            cfg.cookie.name,
            encode_session_cookie(session, signing_key=cfg.signing_key),
        )
        csrf = csrf_token_for_session(session, signing_key=cfg.signing_key)

        requested = self.client.post(
            "/me/api/access-request",
            json={},
            headers={
                "x-ai-gateway-user-action": "1",
                "x-ai-gateway-csrf": csrf,
            },
        )
        self.assertEqual(requested.status_code, 201, requested.text)
        self.assertTrue(requested.json()["app"]["profile_required"])

        denied = self.client.post(
            f"/me/api/apps/{requested.json()['app']['id']}/keys",
            json={},
            headers={
                "x-ai-gateway-user-action": "1",
                "x-ai-gateway-csrf": csrf,
            },
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "profile_required")


if __name__ == "__main__":
    unittest.main()

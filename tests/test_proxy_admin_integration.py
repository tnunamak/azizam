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
from gateway.admin_oidc import CookieConfig, OIDCConfig  # noqa: E402
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
        self.assertIn("Vivid Fish AI Gateway Admin", response.text)
        self.assertIn(">Apps<", response.text)

    def test_admin_dashboard_requires_admin_auth(self) -> None:
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "admin_unauthenticated")

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
            "/admin/login?return_to=/admin", follow_redirects=False
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

        login = self.client.get("/admin/login", follow_redirects=False)
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

    def test_oidc_return_to_is_limited_to_admin_paths(self) -> None:
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
            "/admin/login?return_to=https://evil.example/steal",
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 302, login.text)
        state, record = next(iter(proxy.ADMIN_OIDC_STATES.items()))
        self.assertEqual(record.return_to, "/admin")

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
        self.assertEqual(callback.headers["location"], "/admin")


if __name__ == "__main__":
    unittest.main()

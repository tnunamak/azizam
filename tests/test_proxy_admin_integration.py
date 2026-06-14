from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import re
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
        self.env_patcher = patch.dict(os.environ, {}, clear=False)
        self.env_patcher.start()
        os.environ.pop("AI_GATEWAY_USER_SELF_SERVICE", None)
        os.environ.pop("AI_GATEWAY_AUTO_CREATE_PERSONAL_APP", None)
        proxy.ADMIN_STORE.path = self.db
        proxy.ADMIN_OIDC_STATES.clear()
        proxy.AUTH_ENABLED = True
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
        self.env_patcher.stop()
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

    def test_generate_admin_key_help_does_not_create_credential(self) -> None:
        before = len(admin_store.list_credentials(path=self.db))
        with (
            patch.object(proxy, "ADMIN_DB_PATH", self.db),
            patch.object(sys, "argv", ["proxy.py", "generate-admin-key", "--help"]),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            with self.assertRaises(SystemExit) as raised:
                proxy.handle_cli_command()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Usage: python3 proxy.py generate-admin-key", stdout.getvalue())
        self.assertEqual(len(admin_store.list_credentials(path=self.db)), before)

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
        self.assertIn("No App keys yet", response.text)
        self.assertIn("const policyCatalog=", response.text)
        self.assertIn("profile-workflow-options", response.text)
        self.assertIn("image.generate.flux2-klein-4b", response.text)
        self.assertIn("User-owned Apps and keys", response.text)
        self.assertIn(">Workflows</h3>", response.text)
        self.assertNotIn("Mint runtime key", response.text)

    def test_admin_dashboard_requires_admin_auth(self) -> None:
        # A signed-out BROWSER is sent to sign in, not shown a raw JSON 401. With
        # OIDC off, local-owner sign-in lives on /setup.
        response = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/setup")
        # An API caller (Bearer token) still gets the JSON 401.
        api = self.client.get("/admin", headers={"Authorization": "Bearer nope"})
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.json()["error"]["code"], "admin_unauthenticated")

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
        users = admin_store.list_users(path=self.db)
        self.assertEqual(len(users), 1)
        apps = admin_store.list_apps_for_user(users[0]["id"], path=self.db)
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["kind"], "personal")

    def test_oidc_login_trusted_group_grants_admin_session(self) -> None:
        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset(),
            admin_emails=frozenset({"owner@example.com"}),
            signing_key=b"s" * 32,
            cookie=CookieConfig(secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
            admin_groups=frozenset({"trusted users"}),
            admin_roles=frozenset({"trusted"}),
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie

        self.client.get("/login?return_to=/admin", follow_redirects=False)
        state, record = next(iter(proxy.ADMIN_OIDC_STATES.items()))

        async def fake_exchange(_code, *, config, pkce):
            self.assertEqual(pkce.verifier, record.pkce_verifier)
            return {"id_token": "fake.id.token"}

        async def fake_verify(_id_token, *, config):
            return {
                "iss": cfg.issuer,
                "aud": cfg.client_id,
                "sub": "trusted-subject",
                "email": "simon@appears.blue",
                "email_verified": True,
                "groups": ["/trusted-users"],
                "realm_access": {"roles": ["trusted"]},
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

        dashboard = self.client.get("/admin")
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertIn("simon@appears.blue", dashboard.text)
        user = admin_store.list_users(path=self.db)[0]
        roles = admin_store.list_admin_roles(
            subject_kind="user",
            subject_id=user["id"],
            path=self.db,
        )
        self.assertEqual({role["role"] for role in roles}, {"admin"})

    def test_oidc_login_auto_provision_persists_resolved_default_profile(self) -> None:
        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset({"example.com"}),
            admin_emails=frozenset(),
            signing_key=b"s" * 32,
            cookie=CookieConfig(secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
        )
        profile = admin_store.create_profile(
            "OIDC user default",
            {"id": "oidc-user-default", "protocols": ["openai.chat"]},
            path=self.db,
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie

        login = self.client.get("/login?return_to=/me", follow_redirects=False)
        state, record = next(iter(proxy.ADMIN_OIDC_STATES.items()))

        async def fake_exchange(_code, *, config, pkce):
            self.assertEqual(pkce.verifier, record.pkce_verifier)
            return {"id_token": "fake.id.token"}

        async def fake_verify(_id_token, *, config):
            return {
                "iss": cfg.issuer,
                "aud": cfg.client_id,
                "sub": "oidc-auto-subject",
                "email": "user@example.com",
                "email_verified": True,
                "nonce": record.nonce,
                "exp": 4_000_000_000,
                "iat": 1_700_000_000,
            }

        self.assertEqual(login.status_code, 302, login.text)
        with patch.object(
            proxy,
            "user_defaults_cfg",
            {
                "self_service_apps": True,
                "auto_create_personal_app": True,
                "default_profile_id": profile["id"],
            },
        ), patch.object(proxy, "_oidc_exchange_code", fake_exchange), patch.object(
            proxy, "_oidc_verify_id_token", fake_verify
        ):
            callback = self.client.get(
                f"/admin/oidc/callback?state={state}&code=abc",
                follow_redirects=False,
            )

        self.assertEqual(callback.status_code, 302, callback.text)
        user = next(
            user
            for user in admin_store.list_users(path=self.db)
            if user["issuer"] == "https://idp.example"
            and user["subject"] == "oidc-auto-subject"
        )
        apps = admin_store.list_apps_for_user(user["id"], path=self.db)
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["default_profile_id"], profile["id"])

    def test_status_dashboard_accepts_admin_oidc_session(self) -> None:
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
            "status-owner-subject",
            email="owner@example.com",
            display_name="Owner",
            path=self.db,
        )
        admin_store.assign_admin_role(
            role="owner",
            subject_kind="user",
            subject_id=user["id"],
            granted_by="test",
            path=self.db,
        )
        session = session_from_claims(
            {"sub": user["id"], "email": "owner@example.com"},
            role="owner",
            cookie=cfg.cookie,
        )
        self.client.cookies.set(
            cfg.cookie.name,
            encode_session_cookie(session, signing_key=cfg.signing_key),
        )

        async def fake_status(_config):
            return {"generated_at": 0, "banner": "ok", "errors": {}}

        with patch.object(proxy.status_endpoint, "get_cached_status", fake_status):
            response = self.client.get("/status")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/html")
        self.assertIn("owner@example.com", response.text)
        self.assertIn('href="/me/logout">Sign out</a>', response.text)
        self.assertIn('href="/status" aria-current="page">Status</a>', response.text)

    def test_status_dashboard_redirects_browser_to_login_when_oidc_enabled(self) -> None:
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

        response = self.client.get("/status", follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(response.headers["location"], "/login?return_to=%2Fstatus")

    def test_v1_status_accepts_admin_key(self) -> None:
        async def fake_status(_config):
            return {"generated_at": 0, "banner": "ok", "errors": {}}

        with patch.object(proxy.status_endpoint, "get_cached_status", fake_status):
            response = self.client.get("/v1/status", headers=self._auth())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["banner"], "ok")

    def test_status_routes_reject_unauthenticated_loopback(self) -> None:
        proxy.ADMIN_OIDC_CONFIG = OIDCConfig(
            enabled=False,
            issuer="",
            client_id="",
            client_secret="",
            redirect_uri="",
            scopes=(),
            allowed_email_domains=frozenset(),
            admin_emails=frozenset(),
            signing_key=b"",
            cookie=CookieConfig(secure=False),
        )
        client = TestClient(proxy.app, client=("127.0.0.1", 50000))

        html = client.get("/status")
        self.assertEqual(html.status_code, 401, html.text)
        self.assertEqual(html.json()["error"]["code"], "admin_unauthenticated")

        json_resp = client.get("/v1/status")
        self.assertEqual(json_resp.status_code, 401, json_resp.text)
        self.assertEqual(
            json_resp.json()["error"]["code"],
            "admin_unauthenticated",
        )

    def test_v1_status_rejects_app_runtime_key(self) -> None:
        created = self.client.post(
            "/admin/api/apps",
            json={"name": "status-not-admin"},
            headers=self._auth(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        app_id = created.json()["app"]["id"]
        key_response = self.client.post(
            f"/admin/api/apps/{app_id}/keys",
            json={"label": "App key"},
            headers=self._auth(),
        )
        self.assertEqual(key_response.status_code, 201, key_response.text)
        app_key = key_response.json()["credential"]["api_key"]

        response = self.client.get(
            "/v1/status",
            headers={"Authorization": f"Bearer {app_key}"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "admin_forbidden")

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
        # A signed-in NON-admin browser hitting /admin is sent to their home
        # (the portal), not shown a raw JSON 403.
        admin = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(admin.status_code, 302)
        self.assertEqual(admin.headers["location"], "/")
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

        access = self.client.get(
            f"/me/api/credentials/{credential['id']}/access",
            headers={
                "x-ai-gateway-user-action": "1",
                "x-ai-gateway-csrf": csrf,
            },
        )
        self.assertEqual(access.status_code, 200, access.text)
        self.assertEqual(access.json()["credential"]["id"], credential["id"])
        self.assertIn("capabilities", access.json())

        revoked = self.client.delete(
            f"/me/api/credentials/{credential['id']}",
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

    def test_user_activity_feed_is_owner_scoped_and_receipts_dont_leak(self) -> None:
        # The activity feed and receipt-detail endpoint must show a user ONLY
        # their own Apps' receipts — never another user's. This is the privacy
        # boundary for Phase 2.
        from gateway import receipt_store
        from gateway.receipts import RouteReceipt

        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset({"example.com"}),
            admin_emails=frozenset(),
            signing_key=b"s" * 32,
            cookie=CookieConfig(secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie

        # Isolate the receipts store to a temp DB for this test.
        receipts_db = Path(self.tmp.name) / "receipts.db"
        old_receipts_path = receipt_store.DB_PATH
        receipt_store.DB_PATH = receipts_db
        try:
            receipt_store.init_db(receipts_db)

            alice = admin_store.upsert_oidc_user(
                cfg.issuer, "alice-sub", email="alice@example.com", path=self.db
            )
            bob = admin_store.upsert_oidc_user(
                cfg.issuer, "bob-sub", email="bob@example.com", path=self.db
            )
            app_a = admin_store.create_app(
                "Alice App", kind="personal", owner_user_id=alice["id"], path=self.db
            )
            app_b = admin_store.create_app(
                "Bob App", kind="personal", owner_user_id=bob["id"], path=self.db
            )

            # Two receipts for Alice, one for Bob, one unattributed.
            a1 = receipt_store.record(
                RouteReceipt(protocol="openai.chat", intent="chat.generate",
                             app_id=app_a["id"], status="200"),
                path=receipts_db,
            )
            receipt_store.record(
                RouteReceipt(protocol="openai.chat", intent="chat.generate",
                             app_id=app_a["id"], status="500"),
                path=receipts_db,
            )
            b1 = receipt_store.record(
                RouteReceipt(protocol="openai.chat", intent="chat.generate",
                             app_id=app_b["id"], status="200"),
                path=receipts_db,
            )

            def sign_in(user, email):
                session = session_from_claims(
                    {"sub": user["id"], "email": email},
                    role="none",
                    cookie=cfg.cookie,
                )
                self.client.cookies.set(
                    cfg.cookie.name,
                    encode_session_cookie(session, signing_key=cfg.signing_key),
                )

            # Alice sees exactly her 2 receipts.
            sign_in(alice, "alice@example.com")
            feed = self.client.get("/me/api/activity")
            self.assertEqual(feed.status_code, 200, feed.text)
            self.assertEqual(len(feed.json()["receipts"]), 2)
            # And can open her own receipt detail.
            own = self.client.get(f"/me/api/receipts/{a1}")
            self.assertEqual(own.status_code, 200, own.text)
            self.assertEqual(own.json()["request_id"], a1)
            # But NOT Bob's — reported as not found, never 403.
            cross = self.client.get(f"/me/api/receipts/{b1}")
            self.assertEqual(cross.status_code, 404, cross.text)
            # Client audience: no admin-only fields leak into the detail.
            self.assertNotIn("api_key_name", own.json())
            self.assertNotIn("app_id", own.json())
            self.assertNotIn("metadata", own.json())

            # Bob sees exactly his 1 receipt, and can't read Alice's.
            sign_in(bob, "bob@example.com")
            bob_feed = self.client.get("/me/api/activity")
            self.assertEqual(len(bob_feed.json()["receipts"]), 1)
            self.assertEqual(
                self.client.get(f"/me/api/receipts/{a1}").status_code, 404
            )

            # Signed-out: empty feed, no leak.
            self.client.cookies.clear()
            anon = self.client.get("/me/api/activity")
            self.assertEqual(anon.status_code, 200)
            self.assertFalse(anon.json()["authenticated"])
            self.assertEqual(anon.json()["receipts"], [])
        finally:
            receipt_store.DB_PATH = old_receipts_path

    def test_receipt_deep_link_route_and_detail_for_owner(self) -> None:
        # /r/:id serves the portal page (the JS opens the receipt sheet from the
        # path); /me/api/receipts/:id returns the owner-scoped detail with the
        # "why" fields for a failed call.
        from gateway import receipt_store
        from gateway.receipts import RouteReceipt

        cfg = OIDCConfig(
            enabled=True,
            issuer="https://idp.example",
            client_id="ai-gateway",
            client_secret="secret",
            redirect_uri="http://testserver/admin/oidc/callback",
            scopes=("openid", "email", "profile"),
            allowed_email_domains=frozenset({"example.com"}),
            admin_emails=frozenset(),
            signing_key=b"s" * 32,
            cookie=CookieConfig(secure=False),
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
        )
        proxy.ADMIN_OIDC_CONFIG = cfg
        proxy.ADMIN_AUTH.signing_key = cfg.signing_key
        proxy.ADMIN_AUTH.cookie = cfg.cookie
        receipts_db = Path(self.tmp.name) / "receipts.db"
        old = receipt_store.DB_PATH
        receipt_store.DB_PATH = receipts_db
        try:
            receipt_store.init_db(receipts_db)
            user = admin_store.upsert_oidc_user(
                cfg.issuer, "ada-sub", email="ada@example.com", path=self.db
            )
            app = admin_store.create_app(
                "Ada App", kind="personal", owner_user_id=user["id"], path=self.db
            )
            rcpt = RouteReceipt(
                protocol="openai.chat", intent="chat.completions",
                path="/v1/chat/completions", app_id=app["id"],
                requested_model="gpt-4", status="429",
            )
            rcpt.reject_input("temperature", "not in manifest")
            rcpt.add_fallback_reason("primary backend rate-limited")
            rid = receipt_store.record(rcpt, path=receipts_db)

            session = session_from_claims(
                {"sub": user["id"], "email": "ada@example.com"},
                role="none", cookie=cfg.cookie,
            )
            self.client.cookies.set(
                cfg.cookie.name,
                encode_session_cookie(session, signing_key=cfg.signing_key),
            )
            try:
                # The deep-link route serves the portal HTML (200), not a 404.
                page = self.client.get(f"/r/{rid}")
                self.assertEqual(page.status_code, 200, page.text)
                self.assertIn("openReceiptSheet", page.text)

                # The owner-scoped detail carries the why-fields.
                detail = self.client.get(f"/me/api/receipts/{rid}")
                self.assertEqual(detail.status_code, 200, detail.text)
                body = detail.json()
                self.assertEqual(body["status"], "429")
                self.assertEqual(body["rejected_inputs"]["temperature"], "not in manifest")
                self.assertIn("primary backend rate-limited", body["fallback_reasons"])
                # Client audience: no admin-only fields leak.
                self.assertNotIn("api_key_name", body)
                self.assertNotIn("app_id", body)
            finally:
                self.client.cookies.clear()
        finally:
            receipt_store.DB_PATH = old

    def test_user_portal_self_creates_personal_app_and_mints_key(self) -> None:
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
            "/me/api/apps",
            json={"name": "New user laptop"},
            headers={
                "x-ai-gateway-user-action": "1",
                "x-ai-gateway-csrf": csrf,
            },
        )
        self.assertEqual(requested.status_code, 201, requested.text)
        self.assertFalse(requested.json()["app"]["profile_required"])
        self.assertEqual(
            requested.json()["app"]["effective_profile_id"],
            "default-local",
        )
        self.assertEqual(requested.json()["app"]["name"], "New user laptop")

        minted = self.client.post(
            f"/me/api/apps/{requested.json()['app']['id']}/keys",
            json={"label": "New user laptop"},
            headers={
                "x-ai-gateway-user-action": "1",
                "x-ai-gateway-csrf": csrf,
            },
        )
        self.assertEqual(minted.status_code, 201, minted.text)
        self.assertTrue(minted.json()["credential"]["api_key"].startswith("opk_"))

        capabilities = self.client.get(
            "/gateway/v1/capabilities",
            headers={
                "Authorization": (
                    f"Bearer {minted.json()['credential']['api_key']}"
                )
            },
        )
        self.assertEqual(capabilities.status_code, 200, capabilities.text)

        deleted = self.client.delete(
            f"/me/api/apps/{requested.json()['app']['id']}",
            headers={
                "x-ai-gateway-user-action": "1",
                "x-ai-gateway-csrf": csrf,
            },
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["app"]["revoked_credentials"], 1)

        session_resp = self.client.get("/me/api/session")
        self.assertEqual(session_resp.status_code, 200, session_resp.text)
        self.assertEqual(session_resp.json()["apps"], [])

    def test_user_self_service_flag_blocks_app_and_key_mutations(self) -> None:
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
            "limited-subject",
            email="limited@example.com",
            display_name="Limited",
            path=self.db,
        )
        app = admin_store.create_app(
            "limited-laptop",
            kind="personal",
            owner_user_id=user["id"],
            path=self.db,
        )
        session = session_from_claims(
            {"sub": user["id"], "email": "limited@example.com"},
            role="none",
            cookie=cfg.cookie,
        )
        self.client.cookies.set(
            cfg.cookie.name,
            encode_session_cookie(session, signing_key=cfg.signing_key),
        )
        csrf = csrf_token_for_session(session, signing_key=cfg.signing_key)

        with patch.object(proxy, "user_defaults_cfg", {"self_service_apps": False}):
            created = self.client.post(
                "/me/api/apps",
                json={"name": "Blocked"},
                headers={
                    "x-ai-gateway-user-action": "1",
                    "x-ai-gateway-csrf": csrf,
                },
            )
            self.assertEqual(created.status_code, 403)
            self.assertEqual(created.json()["error"]["code"], "self_service_disabled")

            minted = self.client.post(
                f"/me/api/apps/{app['id']}/keys",
                json={"label": "Blocked"},
                headers={
                    "x-ai-gateway-user-action": "1",
                    "x-ai-gateway-csrf": csrf,
                },
            )
            self.assertEqual(minted.status_code, 403)
            self.assertEqual(minted.json()["error"]["code"], "self_service_disabled")

            deleted = self.client.delete(
                f"/me/api/apps/{app['id']}",
                headers={
                    "x-ai-gateway-user-action": "1",
                    "x-ai-gateway-csrf": csrf,
                },
            )
            self.assertEqual(deleted.status_code, 403)
            self.assertEqual(deleted.json()["error"]["code"], "self_service_disabled")

    def test_self_service_flag_blocks_dynamic_legacy_profile_inheritance(self) -> None:
        user = admin_store.upsert_oidc_user(
            "https://idp.example",
            "existing-subject",
            email="existing@example.com",
            display_name="Existing",
            path=self.db,
        )
        profile = admin_store.create_profile(
            "Existing user default",
            {"id": "existing-user-default", "protocols": ["openai.chat"]},
            path=self.db,
        )
        app = admin_store.create_app(
            "existing-laptop",
            kind="personal",
            owner_user_id=user["id"],
            path=self.db,
        )

        with patch.object(
            proxy,
            "user_defaults_cfg",
            {
                "self_service_apps": True,
                "default_profile_id": profile["id"],
            },
        ):
            self.assertEqual(proxy._inherited_profile_id_for_app(app), profile["id"])

        self.assertIsNone(
            admin_store.get_app(app["id"], path=self.db)["default_profile_id"]
        )

        with patch.object(
            proxy,
            "user_defaults_cfg",
            {
                "self_service_apps": False,
                "default_profile_id": profile["id"],
            },
        ):
            self.assertIsNone(proxy._inherited_profile_id_for_app(app))

    def test_startup_auto_provision_persists_resolved_default_profile(self) -> None:
        user = admin_store.upsert_oidc_user(
            "https://idp.example",
            "startup-subject",
            email="startup@example.com",
            display_name="Startup",
            path=self.db,
        )
        profile = admin_store.create_profile(
            "Startup user default",
            {"id": "startup-user-default", "protocols": ["openai.chat"]},
            path=self.db,
        )

        with patch.object(
            proxy,
            "user_defaults_cfg",
            {
                "self_service_apps": True,
                "auto_create_personal_app": True,
                "default_profile_id": profile["id"],
            },
        ):
            self.assertEqual(proxy._provision_existing_user_personal_apps(), 1)

        apps = admin_store.list_apps_for_user(user["id"], path=self.db)
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["default_profile_id"], profile["id"])

    def test_runtime_profile_mapping_from_oidc_group_overrides_yaml_defaults(self) -> None:
        profile = admin_store.create_profile(
            "Trusted users",
            {"id": "trusted-users", "protocols": ["openai"]},
            path=self.db,
        )
        admin_store.create_runtime_profile_mapping(
            source_kind="oidc_group",
            source_value="/trusted-users",
            profile_id=profile["id"],
            path=self.db,
        )

        with patch.object(
            proxy,
            "user_defaults_cfg",
            {"default_profile_id": "default-local"},
        ):
            self.assertEqual(
                proxy._default_profile_id_for_user(
                    email="simon@example.com",
                    role="none",
                    oidc_groups=["trusted-users"],
                ),
                profile["id"],
            )

    def test_bootstrap_user_defaults_seed_admin_db_runtime_policy_once(self) -> None:
        family = admin_store.create_profile(
            "Family",
            {"id": "family", "protocols": ["openai.chat"]},
            path=self.db,
        )
        admin = admin_store.create_profile(
            "Admin",
            {"id": "admin-runtime", "protocols": ["openai"]},
            path=self.db,
        )

        with patch.object(
            proxy,
            "user_defaults_cfg",
            {
                "self_service_apps": False,
                "auto_create_personal_app": False,
                "default_profile_id": family["id"],
                "role_profile_ids": {"admin": admin["id"]},
                "domain_profile_ids": {"example.com": family["id"]},
            },
        ):
            seeded = proxy._seed_user_defaults_from_bootstrap_config()
            self.assertEqual(seeded, {"settings": 1, "mappings": 3})

            setting = admin_store.get_operator_setting(
                proxy.USER_ACCESS_DEFAULTS_SETTING_KEY,
                path=self.db,
            )
            self.assertIsNotNone(setting)
            self.assertFalse(setting["value"]["self_service_apps"])  # type: ignore[index]
            self.assertFalse(
                proxy._runtime_user_access_defaults()["self_service_apps"]
            )

            mappings = admin_store.list_runtime_profile_mappings(path=self.db)
            observed = {
                (
                    row["source_kind"],
                    row["source_value"],
                    row["profile_id"],
                )
                for row in mappings
            }
            self.assertIn(("all_users", "*", family["id"]), observed)
            self.assertIn(("gateway_role", "admin", admin["id"]), observed)
            self.assertIn(("domain", "example.com", family["id"]), observed)

            seeded_again = proxy._seed_user_defaults_from_bootstrap_config()
            self.assertEqual(seeded_again, {"settings": 0, "mappings": 0})

        actions = [event["action"] for event in admin_store.list_audit(path=self.db)]
        self.assertIn("operator.user_access_defaults.seed", actions)
        self.assertIn("runtime_profile_mapping.seed", actions)

    def test_env_user_defaults_seed_admin_db_without_remaining_runtime_override(self) -> None:
        with patch.object(proxy, "user_defaults_cfg", {}), patch.dict(
            os.environ,
            {
                "AI_GATEWAY_USER_SELF_SERVICE": "false",
                "AI_GATEWAY_AUTO_CREATE_PERSONAL_APP": "false",
            },
            clear=False,
        ):
            seeded = proxy._seed_user_defaults_from_bootstrap_config()
            self.assertEqual(seeded, {"settings": 1, "mappings": 0})

        setting = admin_store.get_operator_setting(
            proxy.USER_ACCESS_DEFAULTS_SETTING_KEY,
            path=self.db,
        )
        self.assertIsNotNone(setting)
        self.assertFalse(setting["value"]["self_service_apps"])  # type: ignore[index]

        with patch.dict(
            os.environ,
            {"AI_GATEWAY_USER_SELF_SERVICE": "true"},
            clear=False,
        ):
            self.assertFalse(proxy._user_self_service_enabled())

    def test_partial_bootstrap_seed_recovers_missing_runtime_mappings(self) -> None:
        family = admin_store.create_profile(
            "Family",
            {"id": "family", "protocols": ["openai.chat"]},
            path=self.db,
        )
        admin_store.put_operator_setting(
            proxy.USER_ACCESS_DEFAULTS_SETTING_KEY,
            {"self_service_apps": True, "auto_create_personal_app": True},
            updated_by="test",
            path=self.db,
        )

        with patch.object(
            proxy,
            "user_defaults_cfg",
            {"domain_profile_ids": {"example.com": family["id"]}},
        ):
            seeded = proxy._seed_user_defaults_from_bootstrap_config()
            resolved = proxy._default_profile_id_for_user(
                email="person@example.com",
                role="none",
            )

        self.assertEqual(seeded, {"settings": 0, "mappings": 1})
        self.assertEqual(resolved, family["id"])

    def test_runtime_user_access_defaults_db_wins_over_bootstrap_config(self) -> None:
        admin_store.put_operator_setting(
            proxy.USER_ACCESS_DEFAULTS_SETTING_KEY,
            {"self_service_apps": False, "auto_create_personal_app": False},
            updated_by="test",
            path=self.db,
        )

        with patch.object(
            proxy,
            "user_defaults_cfg",
            {"self_service_apps": True, "auto_create_personal_app": True},
        ):
            self.assertFalse(proxy._user_self_service_enabled())
            self.assertFalse(proxy._auto_create_personal_app_enabled())

    def test_disabled_seeded_mapping_is_not_reenabled_by_bootstrap_defaults(self) -> None:
        profile = admin_store.create_profile(
            "Family",
            {"id": "family", "protocols": ["openai.chat"]},
            path=self.db,
        )
        mapping = admin_store.create_runtime_profile_mapping(
            source_kind="domain",
            source_value="example.com",
            profile_id=profile["id"],
            path=self.db,
        )
        admin_store.disable_runtime_profile_mapping(mapping["id"], path=self.db)

        with patch.object(
            proxy,
            "user_defaults_cfg",
            {"domain_profile_ids": {"example.com": profile["id"]}},
        ):
            seeded = proxy._seed_user_defaults_from_bootstrap_config()
            resolved = proxy._default_profile_id_for_user(
                email="person@example.com",
                role="none",
            )

        self.assertEqual(seeded, {"settings": 1, "mappings": 0})
        self.assertIsNone(resolved)
        active = admin_store.list_runtime_profile_mappings(path=self.db)
        self.assertEqual(active, [])


class SetupRouteIntegrationTests(unittest.TestCase):
    """Route-level tests for `/setup`.

    Uses a clean admin DB with no service-account admin so the "first-run
    claim" state is reachable. Individual tests promote a user to owner to
    exercise the "setup complete" state.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        self.old_path = proxy.ADMIN_STORE.path
        self.old_oidc = proxy.ADMIN_OIDC_CONFIG
        self.old_signing_key = proxy.ADMIN_AUTH.signing_key
        self.old_cookie = proxy.ADMIN_AUTH.cookie
        self.old_auth_enabled = proxy.AUTH_ENABLED
        proxy.ADMIN_STORE.path = self.db
        proxy.ADMIN_OIDC_STATES.clear()
        proxy.AUTH_ENABLED = True
        proxy.ADMIN_AUTH.signing_key = b"s" * 32
        proxy.ADMIN_AUTH.cookie = CookieConfig(secure=False)
        admin_store.init_db(self.db)
        # No service-account admin key here -> no owner/admin exists yet.
        self.client = TestClient(proxy.app)

    def tearDown(self) -> None:
        proxy.ADMIN_STORE.path = self.old_path
        proxy.ADMIN_OIDC_CONFIG = self.old_oidc
        proxy.ADMIN_AUTH.signing_key = self.old_signing_key
        proxy.ADMIN_AUTH.cookie = self.old_cookie
        proxy.AUTH_ENABLED = self.old_auth_enabled
        proxy.ADMIN_OIDC_STATES.clear()
        self.tmp.cleanup()

    def _enable_oidc(self) -> OIDCConfig:
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
        return cfg

    def test_setup_renders_claim_card_when_oidc_enabled_and_no_owner(self) -> None:
        self._enable_oidc()
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.headers.get("content-type"),
            "text/html; charset=utf-8",
        )
        self.assertIn("Claim this gateway", response.text)
        self.assertIn("Sign in to claim owner", response.text)
        self.assertIn("/login?return_to=%2Fsetup", response.text)
        self.assertIn("first allowed OIDC login", response.text)
        self.assertNotIn("Setup complete", response.text)

    def test_setup_explains_oidc_setup_when_disabled_and_no_owner(self) -> None:
        # OIDC is disabled by default in this fixture (no _enable_oidc()).
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200, response.text)
        # The primary first-run path is the web local-owner form, not the
        # host-side CLI recipe.
        self.assertIn("Create the local owner account", response.text)
        self.assertIn('action="/setup/local-owner"', response.text)
        # CLI recovery is still reachable but now lives inside a <details>
        # disclosure, never as the headline action.
        self.assertIn("generate-admin-key owner-cli", response.text)
        # No "claim owner" CTA, no /login link.
        self.assertNotIn("Sign in to claim owner", response.text)
        self.assertIn('method="post" action="/setup/claim"', response.text)

    def test_setup_shows_complete_card_when_owner_exists(self) -> None:
        # Bootstrap a service-account owner -> _owner_or_admin_exists() is True.
        create_admin_service_account_key("real-owner", path=self.db)
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("Setup complete", response.text)
        self.assertIn("This gateway has an owner", response.text)
        self.assertIn('href="/"', response.text)
        self.assertIn('href="/admin"', response.text)
        self.assertNotIn("Sign in to claim owner", response.text)

    def test_safe_return_to_accepts_setup(self) -> None:
        self.assertEqual(proxy._safe_admin_return_to("/setup"), "/setup")

    def test_safe_return_to_still_rejects_external_redirects(self) -> None:
        self.assertEqual(
            proxy._safe_admin_return_to("//evil.example/setup"),
            "/admin",
        )

    def test_login_falls_back_to_setup_message_when_oidc_disabled(self) -> None:
        # OIDC disabled -> /login returns a 501 that points operators to /setup
        # and clarifies that the CLI is recovery, not the normal path.
        response = self.client.get("/login", follow_redirects=False)
        self.assertEqual(response.status_code, 501, response.text)
        body = response.text
        self.assertIn("/setup", body)
        self.assertIn("recovery and headless automation", body)
        # No longer frames the CLI as the primary way in.
        self.assertNotIn(
            "Use `python proxy.py generate-admin-key <name>` for /admin/api",
            body,
        )

    def test_setup_login_link_uses_setup_return_to(self) -> None:
        self._enable_oidc()
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200)
        # The primary CTA's href is /login?return_to=%2Fsetup; after OIDC
        # login the callback redirects back to /setup.
        self.assertIn('href="/login?return_to=%2Fsetup"', response.text)

    def test_setup_login_round_trip_redirects_back_to_setup(self) -> None:
        # Confirm that the return_to value the page emits actually round-trips
        # through /login -> OIDC -> callback -> /setup. Catches regressions if
        # _safe_admin_return_to drops /setup from its whitelist.
        cfg = self._enable_oidc()
        login = self.client.get(
            "/login?return_to=/setup", follow_redirects=False
        )
        self.assertEqual(login.status_code, 302, login.text)
        self.assertIn("https://idp.example/authorize", login.headers["location"])

        state, record = next(iter(proxy.ADMIN_OIDC_STATES.items()))
        self.assertEqual(record.return_to, "/setup")

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
        self.assertEqual(callback.headers["location"], "/setup")

    def test_setup_does_not_leak_private_admin_data(self) -> None:
        create_admin_service_account_key("real-owner", path=self.db)
        response = self.client.get("/setup")
        body = response.text
        # No secret-shaped material should ever appear on /setup.
        for needle in ("sk-", "client_secret_value", "Bearer oak_", "Bearer opk_"):
            self.assertNotIn(needle, body)

    def test_setup_claim_rejects_runtime_app_key_with_clear_error(self) -> None:
        response = self.client.post(
            "/setup/claim",
            data={"admin_key": "opk_not_an_admin_key"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("runtime App key", response.text)
        self.assertIn("admin key", response.text)

    def test_setup_claim_admin_key_creates_browser_admin_session(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "real-owner",
            path=self.db,
        )
        response = self.client.post(
            "/setup/claim",
            data={"admin_key": admin_key},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        self.assertIn(proxy.ADMIN_AUTH.cookie.name, response.headers.get("set-cookie", ""))
        # A signed-in admin landing on /setup is bounced to the portal — the
        # console isn't a place you accidentally land.
        bounced = self.client.get("/setup", follow_redirects=False)
        self.assertEqual(bounced.status_code, 303)
        self.assertEqual(bounced.headers["location"], "/")
        # The operator console is still reachable deliberately for recovery.
        console = self.client.get("/setup?operator=1")
        self.assertEqual(console.status_code, 200, console.text)
        self.assertIn("Mint a key from the console", console.text)
        admin = self.client.get("/admin")
        self.assertEqual(admin.status_code, 200, admin.text)

    def test_setup_signed_in_admin_can_create_first_personal_app_key(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "real-owner",
            path=self.db,
        )
        response = self.client.post(
            "/setup/claim",
            data={"admin_key": admin_key},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)

        async def fake_status(_config):
            return {"backends": [{"name": "loopback", "status": "up"}]}

        with patch.object(proxy.status_endpoint, "get_cached_status", fake_status):
            # The operator console (mint-a-key, doctor) is reached deliberately
            # via ?operator=1 — a plain GET /setup bounces signed-in admins home.
            setup = self.client.get("/setup?operator=1")
            self.assertEqual(setup.status_code, 200, setup.text)
            self.assertIn("Mint a key from the console", setup.text)
            self.assertIn("Is the gateway wired up?", setup.text)
            match = re.search(r'name="csrf_token" value="([^"]+)"', setup.text)
            self.assertIsNotNone(match, setup.text)

            created = self.client.post(
                "/setup/first-app",
                data={
                    "csrf_token": match.group(1),
                    "app_name": "first-app",
                    "key_label": "Laptop",
                },
            )
            self.assertEqual(created.status_code, 201, created.text)

            key_match = re.search(r"opk_[A-Za-z0-9_-]+", created.text)
            self.assertIsNotNone(key_match, created.text)
            api_key = key_match.group(0)
            # Reveal-once copy + a runnable Bearer curl + copy buttons,
            # matching the user portal reveal. Base path is the conventional /v1.
            self.assertIn("shown once and cannot be retrieved", created.text)
            self.assertIn("/v1", created.text)
            self.assertIn("Authorization: Bearer", created.text)
            self.assertIn("setup-copy-btn", created.text)

            apps = admin_store.list_apps(path=self.db)
            self.assertEqual(len(apps), 1)
            self.assertEqual(apps[0]["kind"], "personal")
            credentials = [
                credential
                for credential in admin_store.list_credentials(path=self.db)
                if credential["subject_kind"] == "app"
            ]
            self.assertEqual(len(credentials), 1)
            self.assertEqual(credentials[0]["display_name"], "Laptop")
            audit_actions = [
                event["action"] for event in admin_store.list_audit(path=self.db)
            ]
            self.assertIn("setup.first_app_key", audit_actions)

            refreshed = self.client.get("/setup?operator=1")
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertNotIn(api_key, refreshed.text)
            self.assertIn("Personal App keys: 1", refreshed.text)

    def test_setup_first_app_rejects_stale_csrf(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "real-owner",
            path=self.db,
        )
        response = self.client.post(
            "/setup/claim",
            data={"admin_key": admin_key},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)

        created = self.client.post(
            "/setup/first-app",
            data={"csrf_token": "stale", "app_name": "first-app"},
        )
        self.assertEqual(created.status_code, 403, created.text)
        self.assertIn("setup form expired", created.text)

    def test_setup_local_owner_create_claims_gateway_and_opens_session(self) -> None:
        response = self.client.post(
            "/setup/local-owner",
            data={
                "email": "owner@example.com",
                "password": "correct-horse-battery",
                "password_confirm": "correct-horse-battery",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        # Setup is done — the new owner is handed to the portal (where their key
        # is auto-minted), NOT back to the operator /setup page.
        self.assertEqual(response.headers["location"], "/")
        self.assertIn(
            proxy.ADMIN_AUTH.cookie.name, response.headers.get("set-cookie", "")
        )

        users = admin_store.list_users(path=self.db)
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["email"], "owner@example.com")
        self.assertEqual(users[0]["issuer"], "local")

        roles = admin_store.list_admin_roles(
            subject_kind="user",
            subject_id=users[0]["id"],
            path=self.db,
        )
        self.assertEqual([row["role"] for row in roles], ["owner"])

        verifier = admin_store.get_local_password_verifier(
            users[0]["id"], path=self.db
        )
        self.assertIsNotNone(verifier)
        # The persisted verifier is never the plaintext password.
        self.assertNotIn("correct-horse-battery", str(verifier))

        # The freshly-created owner, now signed in, is bounced from /setup to
        # their portal — no "operator setup" card to dismiss.
        setup = self.client.get("/setup", follow_redirects=False)
        self.assertEqual(setup.status_code, 303)
        self.assertEqual(setup.headers["location"], "/")
        try:
            self.client.cookies.clear()
        except Exception:
            pass

    def test_setup_local_owner_accepts_plain_username_without_at_sign(self) -> None:
        # A self-hoster shouldn't have to invent an email for a break-glass
        # owner. A plain username (no "@") creates and signs back in.
        create = self.client.post(
            "/setup/local-owner",
            data={
                "email": "owner",
                "password": "correct-horse-battery",
                "password_confirm": "correct-horse-battery",
            },
            follow_redirects=False,
        )
        self.assertEqual(create.status_code, 303, create.text)

        users = admin_store.list_users(path=self.db)
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["email"], "owner")

        # Sign back in with the same username after clearing the session.
        try:
            self.client.cookies.clear()
        except Exception:
            pass
        login = self.client.post(
            "/setup/local-login",
            data={"email": "owner", "password": "correct-horse-battery"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 303, login.text)
        self.assertIn(
            proxy.ADMIN_AUTH.cookie.name, login.headers.get("set-cookie", "")
        )
        try:
            self.client.cookies.clear()
        except Exception:
            pass

    def test_setup_local_owner_create_rejects_empty_identifier(self) -> None:
        response = self.client.post(
            "/setup/local-owner",
            data={
                "email": "",
                "password": "correct-horse-battery",
                "password_confirm": "correct-horse-battery",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("email or username", response.text)
        self.assertEqual(admin_store.list_users(path=self.db), [])

    def test_setup_local_owner_create_rejects_mismatched_passwords(self) -> None:
        response = self.client.post(
            "/setup/local-owner",
            data={
                "email": "owner@example.com",
                "password": "correct-horse-battery",
                "password_confirm": "wrong-confirm-value",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("did not match", response.text)
        # The submitted email is preserved so the operator doesn't retype it.
        self.assertIn("owner@example.com", response.text)
        self.assertEqual(admin_store.list_users(path=self.db), [])

    def test_setup_local_owner_create_allows_short_passwords(self) -> None:
        # No password-length policy: the operator picks their own password for
        # their own break-glass account. A short one is accepted.
        response = self.client.post(
            "/setup/local-owner",
            data={
                "email": "owner@example.com",
                "password": "short",
                "password_confirm": "short",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        users = admin_store.list_users(path=self.db)
        self.assertEqual(len(users), 1)
        try:
            self.client.cookies.clear()
        except Exception:
            pass

    def test_setup_local_owner_create_blocked_once_owner_exists(self) -> None:
        create_admin_service_account_key("real-owner", path=self.db)
        response = self.client.post(
            "/setup/local-owner",
            data={
                "email": "second-owner@example.com",
                "password": "correct-horse-battery",
                "password_confirm": "correct-horse-battery",
            },
            follow_redirects=False,
        )
        # Idempotent: we 303 back to /setup rather than letting a second
        # operator silently mint a second owner during first-run.
        self.assertEqual(response.status_code, 303, response.text)
        users = admin_store.list_users(path=self.db)
        self.assertNotIn(
            "second-owner@example.com",
            {user["email"] for user in users},
        )

    def test_setup_local_login_signs_returning_owner_back_in(self) -> None:
        create = self.client.post(
            "/setup/local-owner",
            data={
                "email": "owner@example.com",
                "password": "correct-horse-battery",
                "password_confirm": "correct-horse-battery",
            },
            follow_redirects=False,
        )
        self.assertEqual(create.status_code, 303, create.text)
        # Drop the session cookie so we hit the "owner exists, viewer not
        # signed in" branch on /setup.
        self.client.cookies.clear()

        setup = self.client.get("/setup")
        self.assertEqual(setup.status_code, 200, setup.text)
        self.assertIn('action="/setup/local-login"', setup.text)

        signin = self.client.post(
            "/setup/local-login",
            data={
                "email": "owner@example.com",
                "password": "correct-horse-battery",
            },
            follow_redirects=False,
        )
        self.assertEqual(signin.status_code, 303, signin.text)
        # A returning owner lands on the portal, their home — not /setup.
        self.assertEqual(signin.headers["location"], "/")
        self.assertIn(
            proxy.ADMIN_AUTH.cookie.name, signin.headers.get("set-cookie", "")
        )
        # And a revisit to /setup bounces them to the portal (not a card).
        landed = self.client.get("/setup", follow_redirects=False)
        self.assertEqual(landed.status_code, 303)
        self.assertEqual(landed.headers["location"], "/")
        self.client.cookies.clear()

    def test_setup_local_login_rejects_wrong_password(self) -> None:
        self.client.post(
            "/setup/local-owner",
            data={
                "email": "owner@example.com",
                "password": "correct-horse-battery",
                "password_confirm": "correct-horse-battery",
            },
            follow_redirects=False,
        )
        self.client.cookies.clear()

        bad = self.client.post(
            "/setup/local-login",
            data={
                "email": "owner@example.com",
                "password": "definitely-not-it",
            },
            follow_redirects=False,
        )
        self.assertEqual(bad.status_code, 401, bad.text)
        self.assertIn("did not match", bad.text)
        # The prompt does not leak whether the email exists; same generic
        # error for both wrong-email and wrong-password.
        missing = self.client.post(
            "/setup/local-login",
            data={
                "email": "ghost@example.com",
                "password": "definitely-not-it",
            },
            follow_redirects=False,
        )
        self.assertEqual(missing.status_code, 401, missing.text)
        self.assertIn("did not match", missing.text)

    def test_session_signing_key_persists_without_env_var(self) -> None:
        # The happy first-run path: no env var, no proxy_config secret —
        # _admin_session_signing_key() should still return a stable, on-disk
        # key so /setup can issue and verify the browser cookie.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "admin.db"
            old_db_path = proxy.ADMIN_DB_PATH

            def fake_db_path() -> Path:
                return db_path

            with patch.object(proxy, "_admin_db_path", fake_db_path), patch.object(
                proxy, "_admin_session_signing_key_from_config", lambda: None
            ):
                first = proxy._admin_session_signing_key()
                second = proxy._admin_session_signing_key()
            self.assertIsNotNone(first)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(first, second)
            self.assertTrue((tmp_path / "admin_session_key").exists())
            # File permissions are owner-only on platforms that honor chmod.
            mode = (tmp_path / "admin_session_key").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
            # Sanity: restoring the real db path doesn't matter because we
            # patched _admin_db_path, but make the test independent anyway.
            self.assertEqual(old_db_path, proxy.ADMIN_DB_PATH)

    def test_signed_in_admin_sees_actor_label_and_sign_out_on_operator_console(self) -> None:
        # A plain GET /setup bounces a signed-in admin (real user) to the portal;
        # the operator console (?operator=1) still carries their label + Sign out.
        cfg = self._enable_oidc()
        user = admin_store.upsert_oidc_user(
            cfg.issuer, "owner-sub", email="owner@example.com", path=self.db
        )
        session = session_from_claims(
            {"sub": user["id"], "email": "owner@example.com"},
            role="owner",
            cookie=cfg.cookie,
        )
        cookie_value = encode_session_cookie(session, signing_key=cfg.signing_key)
        self.client.cookies.set(cfg.cookie.name, cookie_value)
        try:
            bounced = self.client.get("/setup", follow_redirects=False)
            self.assertEqual(bounced.status_code, 303)
            self.assertEqual(bounced.headers["location"], "/")
            response = self.client.get("/setup?operator=1")
        finally:
            self.client.cookies.clear()
        self.assertEqual(response.status_code, 200, response.text)
        # The operator console carries the actor label + a working Sign out, but
        # no "you own this gateway" hero — just the tools.
        self.assertNotIn("You own this gateway", response.text)
        self.assertIn("owner@example.com", response.text)
        self.assertIn("Sign out", response.text)
        self.assertIn('href="/logout"', response.text)

    def test_stale_cookie_for_missing_user_does_not_loop_on_setup(self) -> None:
        # Regression: a cookie that's signed + has an admin role but whose user
        # no longer exists (e.g. a prior instance's DB was reset) must NOT
        # redirect /setup -> / (that loops, since / can't resolve the user and
        # shows the door). It falls through to the sign-in form so the operator
        # can sign in fresh.
        cfg = self._enable_oidc()
        session = session_from_claims(
            {"sub": "ghost-user-id-not-in-store", "email": "ghost@example.com"},
            role="owner",
            cookie=cfg.cookie,
        )
        self.client.cookies.set(
            cfg.cookie.name,
            encode_session_cookie(session, signing_key=cfg.signing_key),
        )
        try:
            resp = self.client.get("/setup", follow_redirects=False)
        finally:
            self.client.cookies.clear()
        # No redirect — the page renders so the loop can't happen.
        self.assertEqual(resp.status_code, 200, resp.text)


class AdminEffectiveProfileTests(unittest.TestCase):
    """effective_profile_* fields on admin app responses."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        self.old_path = proxy.ADMIN_STORE.path
        proxy.ADMIN_STORE.path = self.db
        admin_store.init_db(self.db)
        self.admin_key, _, _ = create_admin_service_account_key(
            "test-owner", path=self.db
        )
        self.client = TestClient(proxy.app)

    def tearDown(self) -> None:
        proxy.ADMIN_STORE.path = self.old_path
        self.tmp.cleanup()

    def _auth(self):
        return {"Authorization": f"Bearer {self.admin_key}"}

    def test_app_without_profile_gets_fallback_effective_profile(self) -> None:
        created = self.client.post(
            "/admin/api/apps",
            json={"name": "No Profile App"},
            headers=self._auth(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        app = created.json()["app"]
        self.assertIsNone(app.get("default_profile_id"))
        self.assertEqual(app["effective_profile_id"], "default-local")
        self.assertEqual(app["effective_profile_source"], "fallback")

        listed = self.client.get("/admin/api/apps", headers=self._auth())
        self.assertEqual(listed.status_code, 200)
        match = next(a for a in listed.json()["apps"] if a["id"] == app["id"])
        self.assertEqual(match["effective_profile_id"], "default-local")
        self.assertEqual(match["effective_profile_source"], "fallback")

    def test_app_with_explicit_profile_gets_app_default_effective_profile(self) -> None:
        profile = admin_store.create_profile(
            "Test Profile",
            {"id": "test-profile", "protocols": ["openai.chat"]},
            path=self.db,
        )
        created = self.client.post(
            "/admin/api/apps",
            json={"name": "Profiled App", "profile_id": profile["id"]},
            headers=self._auth(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        app = created.json()["app"]
        self.assertEqual(app["effective_profile_id"], profile["id"])
        self.assertEqual(app["effective_profile_name"], "Test Profile")
        self.assertEqual(app["effective_profile_source"], "app_default")


class AdminCredentialDeleteAliasTests(unittest.TestCase):
    """DELETE /admin/api/credentials/{id} as alias for revoke."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        self.old_path = proxy.ADMIN_STORE.path
        proxy.ADMIN_STORE.path = self.db
        admin_store.init_db(self.db)
        self.admin_key, _, _ = create_admin_service_account_key(
            "test-owner", path=self.db
        )
        self.client = TestClient(proxy.app)

    def tearDown(self) -> None:
        proxy.ADMIN_STORE.path = self.old_path
        self.tmp.cleanup()

    def _auth(self):
        return {"Authorization": f"Bearer {self.admin_key}"}

    def test_delete_credential_revokes_it(self) -> None:
        app = self.client.post(
            "/admin/api/apps", json={"name": "App"}, headers=self._auth()
        ).json()["app"]
        cred = self.client.post(
            f"/admin/api/apps/{app['id']}/keys",
            json={"display_name": "key1"},
            headers=self._auth(),
        ).json()["credential"]

        resp = self.client.delete(
            f"/admin/api/credentials/{cred['id']}",
            headers=self._auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNotNone(resp.json()["credential"].get("revoked_at"))

    def test_delete_credential_404_for_missing(self) -> None:
        resp = self.client.delete(
            "/admin/api/credentials/cred-does-not-exist",
            headers=self._auth(),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "credential_not_found")

    def test_post_revoke_still_works(self) -> None:
        app = self.client.post(
            "/admin/api/apps", json={"name": "App2"}, headers=self._auth()
        ).json()["app"]
        cred = self.client.post(
            f"/admin/api/apps/{app['id']}/keys",
            json={"display_name": "key2"},
            headers=self._auth(),
        ).json()["credential"]

        resp = self.client.post(
            f"/admin/api/credentials/{cred['id']}/revoke",
            headers=self._auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNotNone(resp.json()["credential"].get("revoked_at"))


class AdminAuditEventsEndpointTests(unittest.TestCase):
    """GET /admin/api/audit-events as queryable audit log."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        self.old_path = proxy.ADMIN_STORE.path
        proxy.ADMIN_STORE.path = self.db
        admin_store.init_db(self.db)
        self.admin_key, _, _ = create_admin_service_account_key(
            "test-owner", path=self.db
        )
        self.client = TestClient(proxy.app)

    def tearDown(self) -> None:
        proxy.ADMIN_STORE.path = self.old_path
        self.tmp.cleanup()

    def _auth(self):
        return {"Authorization": f"Bearer {self.admin_key}"}

    def test_audit_events_returns_recent_events(self) -> None:
        self.client.post(
            "/admin/api/apps", json={"name": "Audit App"}, headers=self._auth()
        )
        resp = self.client.get("/admin/api/audit-events", headers=self._auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        # New response shape: {records, next_cursor}
        self.assertIn("records", body)
        self.assertIn("next_cursor", body)
        actions = [e["action"] for e in body["records"]]
        self.assertIn("app.create", actions)

    def test_audit_events_matches_audit_endpoint(self) -> None:
        r1 = self.client.get("/admin/api/audit", headers=self._auth())
        r2 = self.client.get("/admin/api/audit-events", headers=self._auth())
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["records"], r2.json()["records"])

    def test_audit_events_limit_param(self) -> None:
        for i in range(5):
            self.client.post(
                "/admin/api/apps", json={"name": f"App {i}"}, headers=self._auth()
            )
        resp = self.client.get(
            "/admin/api/audit-events?limit=2", headers=self._auth()
        )
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(resp.json()["records"]), 2)


class ConfigExportTests(unittest.TestCase):
    """Tests for GET /admin/api/config/export (Job A, ENGINE_REGISTRY_DESIGN §5.2)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        self.old_path = proxy.ADMIN_STORE.path
        self.old_signing_key = proxy.ADMIN_AUTH.signing_key
        self.old_cookie = proxy.ADMIN_AUTH.cookie
        self.old_auth_enabled = proxy.AUTH_ENABLED
        proxy.ADMIN_STORE.path = self.db
        proxy.AUTH_ENABLED = True
        admin_store.init_db(self.db)
        self.admin_key, _, _ = create_admin_service_account_key(
            "test-owner", path=self.db
        )
        self.client = TestClient(proxy.app)

    def tearDown(self) -> None:
        proxy.ADMIN_STORE.path = self.old_path
        proxy.ADMIN_AUTH.signing_key = self.old_signing_key
        proxy.ADMIN_AUTH.cookie = self.old_cookie
        proxy.AUTH_ENABLED = self.old_auth_enabled
        self.tmp.cleanup()

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.admin_key}"}

    def test_export_requires_admin_auth(self) -> None:
        """Unauthenticated request must be rejected with 401."""
        resp = self.client.get("/admin/api/config/export")
        self.assertEqual(resp.status_code, 401)

    def test_export_returns_valid_yaml(self) -> None:
        """Authenticated request returns 200 with parseable YAML content."""
        import yaml

        resp = self.client.get("/admin/api/config/export", headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        # Must be parseable YAML
        doc = yaml.safe_load(resp.text)
        self.assertIsInstance(doc, dict)

    def test_export_contains_services_key(self) -> None:
        """Exported YAML has a top-level 'services' mapping."""
        import yaml

        resp = self.client.get("/admin/api/config/export", headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        doc = yaml.safe_load(resp.text)
        self.assertIn("services", doc)
        self.assertIsInstance(doc["services"], dict)

    def test_export_includes_seeded_backends(self) -> None:
        """After adding a service via the admin API, it appears in the export."""
        import yaml

        # Add a service via the admin API (same path the UI uses)
        add_resp = self.client.post(
            "/admin/api/operator/services",
            json={
                "name": "Test vLLM",
                "base_url": "http://127.0.0.1:9000/v1",
                "capabilities": ["chat", "completions"],
                "protocol": "openai-compatible",
            },
            headers=self._auth(),
        )
        self.assertIn(add_resp.status_code, (200, 201))

        resp = self.client.get("/admin/api/config/export", headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        doc = yaml.safe_load(resp.text)
        # At least one service should now be present
        self.assertGreater(len(doc["services"]), 0)
        # Connection config present on at least one service
        some_service = next(iter(doc["services"].values()))
        self.assertIn("config", some_service)

    def test_export_excludes_ephemeral_keys(self) -> None:
        """Export must not contain health, last_used_at, vram, or probe fields."""
        import yaml

        resp = self.client.get("/admin/api/config/export", headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        doc = yaml.safe_load(resp.text)

        ephemeral = {
            "health", "last_used", "last_used_at", "vram", "vram_used",
            "vram_gb_used", "probed_at", "probe_window", "health_history",
            "last_error", "live_status", "runtime",
        }
        raw_text = resp.text
        # Quick text scan: none of the ephemeral keys should appear as YAML keys
        for key in ephemeral:
            # A YAML mapping key appears as "  key:" or "key:" at line start
            self.assertNotRegex(
                raw_text,
                rf"(?m)^\s*{re.escape(key)}\s*:",
                msg=f"Ephemeral key '{key}' must not appear in export",
            )

    def test_export_content_disposition_suggests_filename(self) -> None:
        """Response includes Content-Disposition header with a .yml filename."""
        resp = self.client.get("/admin/api/config/export", headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        cd = resp.headers.get("content-disposition", "")
        self.assertIn(".yml", cd)


if __name__ == "__main__":
    unittest.main()

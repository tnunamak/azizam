"""Tests for the normal-user account/runtime-key view."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store  # noqa: E402
from gateway.admin_oidc import AdminSession  # noqa: E402
from gateway.runtime_auth import hash_api_key  # noqa: E402
from gateway.user_runtime import (  # noqa: E402
    UserPortalError,
    build_user_portal_snapshot,
    create_user_app,
    delete_user_app,
    mint_user_app_key,
    request_user_access,
    revoke_user_app_key,
)


class UserRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.db)
        self.user = admin_store.upsert_oidc_user(
            "https://idp.example",
            "user-sub",
            email="user@example.com",
            display_name="User Example",
            path=self.db,
        )
        self.profile = admin_store.create_profile(
            "chat-only",
            {"id": "chat-only", "protocols": ["openai.chat"]},
            path=self.db,
        )
        self.app = admin_store.create_app(
            "user-laptop",
            kind="personal",
            owner_user_id=self.user["id"],
            default_profile_id=self.profile["id"],
            path=self.db,
        )
        self.session = AdminSession(
            subject=self.user["id"],
            email="user@example.com",
            role="none",
            issued_at=1,
            expires_at=4_000_000_000,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_snapshot_lists_owned_apps_and_sanitized_credentials(self) -> None:
        admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app["id"],
            hash=hash_api_key("opk_secret"),
            prefix="opk_live_abcd",
            display_name="Laptop",
            path=self.db,
        )

        snapshot = build_user_portal_snapshot(self.session, path=self.db)

        self.assertTrue(snapshot["authenticated"])
        self.assertEqual(snapshot["user"]["email"], "user@example.com")
        self.assertEqual(snapshot["apps"][0]["name"], "user-laptop")
        self.assertEqual(snapshot["apps"][0]["profile"]["id"], self.profile["id"])
        rendered = repr(snapshot)
        self.assertNotIn("hash", rendered)
        self.assertNotIn("opk_secret", rendered)
        self.assertNotIn("api_key", rendered)

    def test_mint_and_revoke_user_owned_app_key(self) -> None:
        created = mint_user_app_key(
            self.session,
            self.app["id"],
            label="Phone",
            path=self.db,
        )
        self.assertTrue(created["api_key"].startswith("opk_"))
        self.assertEqual(created["display_name"], "Phone")
        self.assertNotIn("hash", created)

        revoked = revoke_user_app_key(
            self.session,
            created["id"],
            path=self.db,
        )
        self.assertFalse(revoked["active"])
        self.assertIsNotNone(revoked["revoked_at"])
        snapshot = build_user_portal_snapshot(self.session, path=self.db)
        self.assertEqual(snapshot["apps"][0]["credentials"], [])
        self.assertEqual(snapshot["apps"][0]["active_credentials"], 0)

    def test_access_request_creates_self_service_app_with_inherited_mint_power(self) -> None:
        new_user = admin_store.upsert_oidc_user(
            "https://idp.example",
            "new-user-sub",
            email="new@example.com",
            display_name="New User",
            path=self.db,
        )
        session = AdminSession(
            subject=new_user["id"],
            email="new@example.com",
            role="none",
            issued_at=1,
            expires_at=4_000_000_000,
        )

        requested = request_user_access(session, path=self.db)
        self.assertFalse(requested["app"]["profile_required"])
        self.assertEqual(requested["app"]["effective_profile_id"], "default-local")
        snapshot = build_user_portal_snapshot(session, path=self.db)
        self.assertEqual(requested["app"], snapshot["apps"][0])
        self.assertTrue(snapshot["apps"][0]["can_mint_keys"])
        self.assertTrue(snapshot["apps"][0]["inherits_profile"])

        created = mint_user_app_key(session, requested["app"]["id"], path=self.db)
        self.assertTrue(created["api_key"].startswith("opk_"))

    def test_self_service_app_can_inherit_configured_default_profile(self) -> None:
        new_user = admin_store.upsert_oidc_user(
            "https://idp.example",
            "profiled-user-sub",
            email="profiled@example.com",
            display_name="Profiled User",
            path=self.db,
        )
        session = AdminSession(
            subject=new_user["id"],
            email="profiled@example.com",
            role="none",
            issued_at=1,
            expires_at=4_000_000_000,
        )

        requested = request_user_access(
            session,
            default_profile_id=self.profile["id"],
            path=self.db,
        )
        stored = admin_store.get_app(requested["app"]["id"], path=self.db)
        self.assertEqual(stored["default_profile_id"], self.profile["id"])
        self.assertEqual(requested["app"]["default_profile_id"], self.profile["id"])
        self.assertEqual(requested["app"]["profile_id"], self.profile["id"])
        self.assertEqual(requested["app"]["effective_profile_id"], self.profile["id"])
        self.assertFalse(requested["app"]["inherits_profile"])
        snapshot = build_user_portal_snapshot(session, path=self.db)
        self.assertEqual(requested["app"], snapshot["apps"][0])
        self.assertEqual(snapshot["apps"][0]["effective_profile_id"], self.profile["id"])
        self.assertTrue(snapshot["apps"][0]["can_mint_keys"])

    def test_user_can_create_named_apps(self) -> None:
        created = create_user_app(
            self.session,
            name="Open WebUI",
            default_profile_id=self.profile["id"],
            path=self.db,
        )
        stored = admin_store.get_app(created["app"]["id"], path=self.db)
        self.assertEqual(stored["default_profile_id"], self.profile["id"])
        self.assertEqual(created["app"]["name"], "Open WebUI")
        self.assertEqual(created["app"]["default_profile_id"], self.profile["id"])
        self.assertEqual(created["app"]["profile_id"], self.profile["id"])
        self.assertEqual(created["app"]["effective_profile_id"], self.profile["id"])
        self.assertFalse(created["app"]["inherits_profile"])

        apps = admin_store.list_apps_for_user(self.user["id"], path=self.db)
        self.assertEqual({app["name"] for app in apps}, {"user-laptop", "Open WebUI"})
        snapshot_app = next(
            app for app in build_user_portal_snapshot(self.session, path=self.db)["apps"]
            if app["id"] == created["app"]["id"]
        )
        self.assertEqual(created["app"], snapshot_app)

    def test_user_app_name_is_required(self) -> None:
        with self.assertRaises(UserPortalError) as ctx:
            create_user_app(self.session, name="   ", path=self.db)
        self.assertEqual(ctx.exception.code, "app_name_required")

    def test_user_can_delete_owned_app_and_hide_it_from_snapshot(self) -> None:
        credential = mint_user_app_key(
            self.session,
            self.app["id"],
            label="Laptop",
            path=self.db,
        )

        deleted = delete_user_app(self.session, self.app["id"], path=self.db)

        self.assertEqual(deleted["app"]["id"], self.app["id"])
        self.assertEqual(deleted["app"]["revoked_credentials"], 1)
        disabled = admin_store.get_app(self.app["id"], path=self.db)
        self.assertIsNotNone(disabled["disabled_at"])
        stored_credential = admin_store.get_credential(credential["id"], path=self.db)
        self.assertIsNotNone(stored_credential["revoked_at"])
        snapshot = build_user_portal_snapshot(self.session, path=self.db)
        self.assertEqual(snapshot["apps"], [])

    def test_user_cannot_revoke_another_users_app_key(self) -> None:
        other = admin_store.upsert_oidc_user(
            "https://idp.example",
            "other-sub",
            email="other@example.com",
            display_name="Other",
            path=self.db,
        )
        other_app = admin_store.create_app(
            "other-laptop",
            kind="personal",
            owner_user_id=other["id"],
            path=self.db,
        )
        credential = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=other_app["id"],
            hash=hash_api_key("opk_other_secret"),
            prefix="opk_other_prefix",
            path=self.db,
        )

        with self.assertRaises(UserPortalError) as ctx:
            revoke_user_app_key(self.session, credential["id"], path=self.db)
        self.assertEqual(ctx.exception.code, "app_not_found")


if __name__ == "__main__":
    unittest.main()

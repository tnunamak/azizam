"""Tests for gateway.admin_store — durable admin/control-plane store.

Uses a temporary SQLite file per test. Covers schema invariants and the
"give my brother a key" flow end-to-end against the durable store.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store  # noqa: E402


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class AdminStoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TestSchema(AdminStoreTestBase):
    def test_schema_version_set(self) -> None:
        self.assertEqual(
            admin_store.schema_version(self.db), admin_store.SCHEMA_VERSION
        )

    def test_init_db_is_idempotent(self) -> None:
        admin_store.init_db(self.db)
        admin_store.init_db(self.db)
        self.assertEqual(
            admin_store.schema_version(self.db), admin_store.SCHEMA_VERSION
        )

    def test_id_prefixes(self) -> None:
        user = admin_store.upsert_oidc_user(
            "https://accounts.google.com", "tim-sub", path=self.db
        )
        self.assertTrue(user["id"].startswith("usr_"))

        svc = admin_store.create_service_account("ci-runner", path=self.db)
        self.assertTrue(svc["id"].startswith("svc_"))

        prof = admin_store.create_profile("p", {"id": "p"}, path=self.db)
        self.assertTrue(prof["id"].startswith("prof_"))

        app = admin_store.ensure_personal_app_for_user(user["id"], path=self.db)
        self.assertTrue(app["id"].startswith("app_"))

        cred = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=app["id"],
            hash=_hash("s"),
            prefix="sk_test",
            path=self.db,
        )
        self.assertTrue(cred["id"].startswith("cred_"))

        aud = admin_store.record_audit("ping", path=self.db)
        self.assertTrue(aud["id"].startswith("aud_"))


class TestOidcUpsert(AdminStoreTestBase):
    def test_first_login_creates_user(self) -> None:
        user = admin_store.upsert_oidc_user(
            "https://accounts.google.com",
            "1234",
            email="tim@example.com",
            display_name="Tim",
            now=1000,
            path=self.db,
        )
        self.assertEqual(user["email"], "tim@example.com")
        self.assertEqual(user["display_name"], "Tim")
        self.assertEqual(user["created_at"], 1000)
        self.assertEqual(user["last_login_at"], 1000)

    def test_relogin_preserves_id_and_updates_last_login(self) -> None:
        first = admin_store.upsert_oidc_user(
            "iss", "sub", email="a@x", display_name="A", now=1000, path=self.db
        )
        second = admin_store.upsert_oidc_user(
            "iss",
            "sub",
            email="a2@x",
            display_name="A2",
            now=2000,
            path=self.db,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["email"], "a2@x")
        self.assertEqual(second["display_name"], "A2")
        self.assertEqual(second["created_at"], 1000)
        self.assertEqual(second["last_login_at"], 2000)

    def test_distinct_issuer_or_subject_is_distinct_user(self) -> None:
        a = admin_store.upsert_oidc_user("iss-a", "sub", path=self.db)
        b = admin_store.upsert_oidc_user("iss-b", "sub", path=self.db)
        c = admin_store.upsert_oidc_user("iss-a", "other", path=self.db)
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotEqual(a["id"], c["id"])

    def test_relogin_without_new_email_preserves_old(self) -> None:
        admin_store.upsert_oidc_user(
            "iss", "sub", email="keep@x", display_name="K", path=self.db
        )
        again = admin_store.upsert_oidc_user("iss", "sub", path=self.db)
        self.assertEqual(again["email"], "keep@x")
        self.assertEqual(again["display_name"], "K")


class TestPersonalApp(AdminStoreTestBase):
    def test_ensure_is_idempotent(self) -> None:
        user = admin_store.upsert_oidc_user("iss", "sub", path=self.db)
        a = admin_store.ensure_personal_app_for_user(user["id"], path=self.db)
        b = admin_store.ensure_personal_app_for_user(user["id"], path=self.db)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["kind"], "personal")
        self.assertEqual(a["owner_user_id"], user["id"])

    def test_personal_app_requires_owner(self) -> None:
        with self.assertRaises(ValueError):
            admin_store.create_app("orphan", kind="personal", path=self.db)


class TestCredentialInvariants(AdminStoreTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.user = admin_store.upsert_oidc_user("iss", "sub", path=self.db)
        self.app = admin_store.ensure_personal_app_for_user(
            self.user["id"], path=self.db
        )
        self.svc = admin_store.create_service_account("ci", path=self.db)

    def test_data_plane_credential_attaches_to_app(self) -> None:
        cred = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app["id"],
            hash=_hash("dp"),
            prefix="sk_live",
            path=self.db,
        )
        self.assertEqual(cred["subject_kind"], "app")
        self.assertEqual(cred["app_id"], self.app["id"])

    def test_admin_credential_must_not_have_app_id(self) -> None:
        with self.assertRaises(ValueError):
            admin_store.create_credential_hash(
                subject_kind="user",
                subject_id=self.user["id"],
                app_id=self.app["id"],
                hash=_hash("u"),
                prefix="adm_live",
                path=self.db,
            )

    def test_admin_credential_for_user(self) -> None:
        cred = admin_store.create_credential_hash(
            subject_kind="user",
            subject_id=self.user["id"],
            hash=_hash("adm"),
            prefix="adm_live",
            path=self.db,
        )
        self.assertEqual(cred["subject_kind"], "user")
        self.assertIsNone(cred["app_id"])

    def test_admin_credential_for_service_account(self) -> None:
        cred = admin_store.create_credential_hash(
            subject_kind="service_account",
            subject_id=self.svc["id"],
            hash=_hash("sa"),
            prefix="adm_live",
            path=self.db,
        )
        self.assertEqual(cred["subject_kind"], "service_account")
        self.assertIsNone(cred["app_id"])

    def test_data_plane_app_mismatch_rejected(self) -> None:
        other = admin_store.create_service_account("other", path=self.db)
        other_app = admin_store.create_app(
            "svc-app",
            kind="service",
            owner_service_account_id=other["id"],
            path=self.db,
        )
        with self.assertRaises(ValueError):
            admin_store.create_credential_hash(
                subject_kind="app",
                subject_id=self.app["id"],
                app_id=other_app["id"],
                hash=_hash("mismatch"),
                prefix="sk",
                path=self.db,
            )

    def test_unknown_subject_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            admin_store.create_credential_hash(
                subject_kind="alien",
                subject_id="x",
                hash=_hash("x"),
                prefix="x",
                path=self.db,
            )

    def test_raw_secret_is_never_stored(self) -> None:
        """Defence-in-depth: the literal secret string must not be on disk."""
        secret = "supersekrit-do-not-leak"
        admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app["id"],
            hash=_hash(secret),
            prefix="sk",
            path=self.db,
        )
        with open(self.db, "rb") as f:
            blob = f.read()
        self.assertNotIn(secret.encode("utf-8"), blob)


class TestCredentialLifecycle(AdminStoreTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.user = admin_store.upsert_oidc_user("iss", "sub", path=self.db)
        self.app = admin_store.ensure_personal_app_for_user(
            self.user["id"], path=self.db
        )

    def test_revoke_omits_from_active_list(self) -> None:
        keep = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app["id"],
            hash=_hash("keep"),
            prefix="sk",
            path=self.db,
        )
        gone = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app["id"],
            hash=_hash("gone"),
            prefix="sk",
            path=self.db,
        )
        self.assertTrue(admin_store.revoke_credential(gone["id"], path=self.db))
        # second revoke is a no-op
        self.assertFalse(
            admin_store.revoke_credential(gone["id"], path=self.db)
        )
        active = admin_store.list_active_credentials(
            app_id=self.app["id"], path=self.db
        )
        ids = {c["id"] for c in active}
        self.assertIn(keep["id"], ids)
        self.assertNotIn(gone["id"], ids)
        # raw record is still retrievable via get_credential
        revoked = admin_store.get_credential(gone["id"], path=self.db)
        self.assertIsNotNone(revoked)
        self.assertIsNotNone(revoked["revoked_at"])  # type: ignore[index]

    def test_expired_omitted_from_active(self) -> None:
        cred = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app["id"],
            hash=_hash("exp"),
            prefix="sk",
            expires_at=500,
            now=100,
            path=self.db,
        )
        # at t=400 still active
        active = admin_store.list_active_credentials(
            app_id=self.app["id"], now=400, path=self.db
        )
        self.assertIn(cred["id"], {c["id"] for c in active})
        # at t=600 expired
        active = admin_store.list_active_credentials(
            app_id=self.app["id"], now=600, path=self.db
        )
        self.assertNotIn(cred["id"], {c["id"] for c in active})

    def test_find_by_hash(self) -> None:
        cred = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app["id"],
            hash=_hash("findme"),
            prefix="sk",
            path=self.db,
        )
        found = admin_store.find_credential_by_hash(_hash("findme"), path=self.db)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], cred["id"])  # type: ignore[index]
        self.assertIsNone(
            admin_store.find_credential_by_hash("missing", path=self.db)
        )

    def test_scopes_roundtrip(self) -> None:
        cred = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app["id"],
            hash=_hash("scoped"),
            prefix="sk",
            scopes=["chat", "images", "chat"],
            path=self.db,
        )
        self.assertEqual(sorted(cred["scopes"]), ["chat", "images"])


class TestProfilesAndAudit(AdminStoreTestBase):
    def test_profile_doc_roundtrips(self) -> None:
        doc = {
            "id": "local-tier",
            "protocols": ["openai.chat", "openai.images"],
            "fallback": {"cloud_allowed": False},
            "status_visibility": {"level": "standard"},
        }
        prof = admin_store.create_profile(
            "local-tier", doc, description="local-only", path=self.db
        )
        loaded = admin_store.get_profile(prof["id"], path=self.db)
        self.assertEqual(loaded["profile"], doc)  # type: ignore[index]

    def test_audit_event_records_actor_action_target_metadata(self) -> None:
        event = admin_store.record_audit(
            "credential.create",
            actor_kind="user",
            actor_id="usr_abc",
            target_kind="credential",
            target_id="cred_xyz",
            metadata={"prefix": "sk_live_brother"},
            path=self.db,
        )
        listed = admin_store.list_audit(
            target_kind="credential", target_id="cred_xyz", path=self.db
        )
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], event["id"])
        self.assertEqual(listed[0]["action"], "credential.create")
        self.assertEqual(listed[0]["metadata"], {"prefix": "sk_live_brother"})


class TestAdminRoles(AdminStoreTestBase):
    def test_role_must_target_control_plane_subject(self) -> None:
        user = admin_store.upsert_oidc_user("iss", "sub", path=self.db)
        app = admin_store.ensure_personal_app_for_user(user["id"], path=self.db)
        with self.assertRaises(ValueError):
            admin_store.assign_admin_role(
                role="owner",
                subject_kind="app",
                subject_id=app["id"],
                path=self.db,
            )

    def test_reassign_is_idempotent_and_unrevokes(self) -> None:
        user = admin_store.upsert_oidc_user("iss", "sub", path=self.db)
        app = admin_store.ensure_personal_app_for_user(user["id"], path=self.db)
        a = admin_store.assign_admin_role(
            role="owner",
            subject_kind="user",
            subject_id=user["id"],
            scope_app_id=app["id"],
            path=self.db,
        )
        admin_store.revoke_admin_role(a["id"], path=self.db)
        b = admin_store.assign_admin_role(
            role="owner",
            subject_kind="user",
            subject_id=user["id"],
            scope_app_id=app["id"],
            path=self.db,
        )
        self.assertEqual(a["id"], b["id"])
        self.assertIsNone(b["revoked_at"])
        active = admin_store.list_admin_roles(
            subject_id=user["id"], path=self.db
        )
        self.assertEqual(len(active), 1)


class TestBrotherFlow(AdminStoreTestBase):
    """End-to-end: 'give my brother a key' against the durable store.

    Tim logs in via OIDC, creates a profile suitable for his brother,
    materializes his brother as a user, ensures the brother has a personal
    app, mints a data-plane credential for that app, grants the brother an
    owner role over that app, and records an audit event for the whole
    transaction. Every step must produce stable, related rows.
    """

    def test_full_flow(self) -> None:
        tim = admin_store.upsert_oidc_user(
            "https://accounts.google.com",
            "tim-sub",
            email="tim@example.com",
            display_name="Tim",
            now=1000,
            path=self.db,
        )

        brother = admin_store.upsert_oidc_user(
            "https://accounts.google.com",
            "brother-sub",
            email="bro@example.com",
            display_name="Brother",
            now=1100,
            path=self.db,
        )

        profile = admin_store.create_profile(
            "brother-local-only",
            {
                "id": "brother-local-only",
                "protocols": ["openai.chat"],
                "fallback": {"cloud_allowed": False},
                "status_visibility": {"level": "basic"},
            },
            description="local-only chat",
            path=self.db,
        )

        app = admin_store.ensure_personal_app_for_user(
            brother["id"], default_profile_id=profile["id"], path=self.db
        )
        self.assertEqual(app["owner_user_id"], brother["id"])
        self.assertEqual(app["default_profile_id"], profile["id"])

        secret_hash = _hash("sk_live_brother_real_secret")
        cred = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=app["id"],
            profile_id=profile["id"],
            hash=secret_hash,
            prefix="sk_live_brother",
            display_name="brother's editor key",
            path=self.db,
        )
        self.assertEqual(cred["subject_kind"], "app")
        self.assertEqual(cred["app_id"], app["id"])
        self.assertEqual(cred["profile_id"], profile["id"])

        role = admin_store.assign_admin_role(
            role="owner",
            subject_kind="user",
            subject_id=brother["id"],
            scope_app_id=app["id"],
            granted_by=tim["id"],
            path=self.db,
        )
        self.assertEqual(role["scope_app_id"], app["id"])
        self.assertEqual(role["granted_by"], tim["id"])

        audit = admin_store.record_audit(
            "credential.create",
            actor_kind="user",
            actor_id=tim["id"],
            target_kind="credential",
            target_id=cred["id"],
            metadata={
                "brother_user_id": brother["id"],
                "app_id": app["id"],
                "prefix": cred["prefix"],
            },
            path=self.db,
        )

        # Verifications wiring everything together:
        active = admin_store.list_active_credentials(
            app_id=app["id"], path=self.db
        )
        self.assertEqual([c["id"] for c in active], [cred["id"]])

        found = admin_store.find_credential_by_hash(secret_hash, path=self.db)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], cred["id"])  # type: ignore[index]

        roles = admin_store.list_admin_roles(
            subject_id=brother["id"], path=self.db
        )
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0]["scope_app_id"], app["id"])

        log = admin_store.list_audit(
            target_kind="credential", target_id=cred["id"], path=self.db
        )
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["id"], audit["id"])
        self.assertEqual(log[0]["actor_id"], tim["id"])
        self.assertEqual(log[0]["metadata"]["app_id"], app["id"])

        # And revocation cleanly removes the credential from active lookups.
        self.assertTrue(
            admin_store.revoke_credential(cred["id"], path=self.db)
        )
        self.assertEqual(
            admin_store.list_active_credentials(app_id=app["id"], path=self.db),
            [],
        )


if __name__ == "__main__":
    unittest.main()

"""Tests for gateway.admin_store — durable admin/control-plane store.

Uses a temporary SQLite file per test. Covers schema invariants and the
"give my brother a key" flow end-to-end against the durable store.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import accounts, admin_store  # noqa: E402
from tests.support.access import plan_document, service_app, store_plan


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

        plan = store_plan("p", path=self.db)
        self.assertTrue(plan["id"].startswith("plan_"))

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

    def test_ensure_service_account_is_idempotent(self) -> None:
        first = admin_store.ensure_service_account("dashboard", path=self.db)
        second = admin_store.ensure_service_account("dashboard", path=self.db)
        self.assertEqual(first["id"], second["id"])


class TestOperatorSettings(AdminStoreTestBase):
    def test_operator_setting_persists_json_document(self) -> None:
        saved = admin_store.put_operator_setting(
            "media_job_admission_overrides",
            {
                "disabled_intents": ["video.generate"],
                "max_queue_depth": 3,
                "retry_after_seconds": 60,
            },
            updated_by="svc_admin",
            now=1234,
            path=self.db,
        )

        self.assertEqual(saved["key"], "media_job_admission_overrides")
        self.assertEqual(saved["updated_at"], 1234)
        self.assertEqual(saved["updated_by"], "svc_admin")
        self.assertEqual(saved["value"]["max_queue_depth"], 3)

        reopened = admin_store.get_operator_setting(
            "media_job_admission_overrides",
            path=self.db,
        )
        self.assertEqual(reopened, saved)

    def test_operator_setting_updates_same_key(self) -> None:
        admin_store.put_operator_setting(
            "service_availability_overrides",
            {"openai": {"state": "degraded"}},
            now=1,
            path=self.db,
        )
        admin_store.put_operator_setting(
            "service_availability_overrides",
            {"openai": {"state": "maintenance"}},
            now=2,
            path=self.db,
        )

        rows = admin_store.list_operator_settings(path=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["updated_at"], 2)
        self.assertEqual(rows[0]["value"]["openai"]["state"], "maintenance")

    def test_service_registry_protection_preserves_operator_settings_generation_and_audit(self) -> None:
        admin_store.write_service_registry(
            {"schema_version": 1, "services": {"existing": {"id": "existing"}}},
            expected_generation=0,
            updated_by="seed",
            now=1,
            path=self.db,
        )
        admin_store.record_audit(
            "operator.service_registry.seed",
            actor_kind="admin",
            actor_id="seed",
            target_kind="operator_setting",
            target_id="service_registry",
            path=self.db,
        )
        before_registry = admin_store.get_service_registry_with_generation(path=self.db)
        before_audit = admin_store.list_audit(path=self.db)

        with self.assertRaisesRegex(ValueError, "registry transaction primitive"):
            admin_store.put_operator_setting(
                "service_registry",
                {"schema_version": 1, "services": {"replacement": {"id": "replacement"}}},
                updated_by="attacker",
                now=2,
                path=self.db,
            )

        self.assertEqual(
            admin_store.get_service_registry_with_generation(path=self.db),
            before_registry,
        )
        self.assertEqual(admin_store.list_audit(path=self.db), before_audit)

    def test_registry_audit_rolls_back_with_registry_and_generation(self) -> None:
        initial = admin_store.write_service_registry(
            {"schema_version": 1, "services": {}},
            expected_generation=0,
            updated_by="seed",
            now=1,
            path=self.db,
        )
        before = admin_store.get_service_registry_with_generation(path=self.db)
        before_audit = admin_store.list_audit(path=self.db)

        with patch.object(
            admin_store,
            "_record_audit_in_transaction",
            side_effect=RuntimeError("injected audit failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected audit failure"):
                admin_store.write_service_registry_with_audit(
                    {"schema_version": 1, "services": {"new": {"id": "new"}}},
                    expected_generation=initial["operator_settings_generation"],
                    updated_by="writer",
                    audit_action="operator.service_registry.update",
                    audit_actor_kind="admin",
                    audit_actor_id="writer",
                    audit_actor_label="Writer",
                    audit_target_kind="operator_setting",
                    audit_target_id="service_registry",
                    now=2,
                    path=self.db,
                )

        self.assertEqual(
            admin_store.get_service_registry_with_generation(path=self.db), before
        )
        self.assertEqual(admin_store.list_audit(path=self.db), before_audit)


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


class TestRuntimeProfileMappings(AdminStoreTestBase):
    def test_oidc_claims_are_replaced_for_user(self) -> None:
        user = admin_store.upsert_oidc_user("iss", "sub", path=self.db)
        admin_store.replace_user_identity_claims(
            user["id"],
            {
                "oidc_group": ["trusted-users", "family"],
                "oidc_role": ["adminish"],
            },
            path=self.db,
        )
        admin_store.replace_user_identity_claims(
            user["id"],
            {"oidc_group": ["family"], "oidc_role": []},
            path=self.db,
        )

        rows = admin_store.list_user_identity_claims(user["id"], path=self.db)
        self.assertEqual(
            [(row["claim_kind"], row["claim_value"]) for row in rows],
            [("oidc_group", "/family")],
        )


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
            admin_store.create_app("orphan", kind="personal", access_plan="person", path=self.db)

    def test_list_apps_returns_all_apps(self) -> None:
        user = admin_store.upsert_oidc_user("iss", "sub", path=self.db)
        personal = admin_store.ensure_personal_app_for_user(
            user["id"], path=self.db
        )
        service = service_app("svc-app", path=self.db)
        self.assertEqual(
            [app["id"] for app in admin_store.list_apps(path=self.db)],
            [personal["id"], service["id"]],
        )

    def test_update_and_disable_app(self) -> None:
        plan = store_plan("p", path=self.db)
        app = service_app("old", path=self.db)

        updated = admin_store.update_app(
            app["id"],
            name="new",
            access_plan=plan["id"],
            path=self.db,
        )
        self.assertEqual(updated["name"], "new")  # type: ignore[index]
        self.assertEqual(updated["access_plan"], plan["id"])  # type: ignore[index]
        with self.assertRaises(Exception):
            admin_store.update_app(app["id"], access_plan="plan_missing", path=self.db)

        disabled = admin_store.disable_app(app["id"], now=1234, path=self.db)
        self.assertEqual(disabled["disabled_at"], 1234)  # type: ignore[index]


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
        other_app = service_app("svc-app", path=self.db)
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

    def test_touch_last_used_writes_then_debounces(self) -> None:
        cred = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app["id"],
            hash=_hash("used"),
            prefix="opk",
            path=self.db,
        )
        cid = cred["id"]
        # A fresh credential has no last-used timestamp.
        self.assertIsNone(
            admin_store.get_credential(cid, path=self.db)["last_used_at"]  # type: ignore[index]
        )
        # First touch writes.
        self.assertTrue(
            admin_store.touch_credential_last_used(cid, now=1000, path=self.db)
        )
        self.assertEqual(
            admin_store.get_credential(cid, path=self.db)["last_used_at"],  # type: ignore[index]
            1000,
        )
        # A touch within the debounce window is a no-op (write rate is bounded).
        self.assertFalse(
            admin_store.touch_credential_last_used(
                cid, now=1000 + 30, min_interval=60, path=self.db
            )
        )
        self.assertEqual(
            admin_store.get_credential(cid, path=self.db)["last_used_at"],  # type: ignore[index]
            1000,
        )
        # Past the window, it writes the newer timestamp.
        self.assertTrue(
            admin_store.touch_credential_last_used(
                cid, now=1000 + 61, min_interval=60, path=self.db
            )
        )
        self.assertEqual(
            admin_store.get_credential(cid, path=self.db)["last_used_at"],  # type: ignore[index]
            1061,
        )

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

    def test_list_credentials_includes_revoked_for_admin_views(self) -> None:
        cred = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app["id"],
            hash=_hash("listed"),
            prefix="sk",
            path=self.db,
        )
        admin_store.revoke_credential(cred["id"], path=self.db)
        rows = admin_store.list_credentials(path=self.db)
        self.assertEqual([row["id"] for row in rows], [cred["id"]])
        self.assertIsNotNone(rows[0]["revoked_at"])

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


class TestAccessPlansAndAudit(AdminStoreTestBase):
    def test_plan_document_roundtrips(self) -> None:
        doc = plan_document("Local tier", caps=("chat", "image"), receipts="standard")
        plan = admin_store.create_access_plan(doc, path=self.db)
        loaded = admin_store.get_access_plan(plan["id"], path=self.db)
        self.assertEqual(loaded["name"], "Local tier")  # type: ignore[index]
        self.assertEqual({k: v for k, v in loaded["plan"].items() if k != "id"}, doc)  # type: ignore[index]

    def test_update_plan_keeps_its_id_and_refuses_a_taken_name(self) -> None:
        plan = store_plan("old", path=self.db)
        store_plan("taken", path=self.db)
        updated = admin_store.update_access_plan(plan["id"], plan_document("new", caps=("chat",)), path=self.db)
        self.assertEqual((updated["id"], updated["name"]), (plan["id"], "new"))  # type: ignore[index]
        with self.assertRaises(ValueError):
            admin_store.update_access_plan(plan["id"], plan_document("taken"), path=self.db)

    def test_an_assigned_plan_cannot_be_deleted_even_in_sql(self) -> None:
        plan = store_plan("used", path=self.db)
        service_app("holder", path=self.db, plan_id=plan["id"])
        with self.assertRaises(ValueError):
            admin_store.delete_access_plan(plan["id"], path=self.db)
        with admin_store._conn(self.db) as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM access_plans WHERE id = ?", (plan["id"],))

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

    def test_audit_can_hide_service_account_activity_and_count_the_group(self) -> None:
        admin_store.record_audit(
            "credential.create",
            actor_kind="service_account",
            actor_id="svc_deploy",
            path=self.db,
        )
        admin_store.record_audit(
            "credential.create",
            actor_kind="user",
            actor_id="usr_owner",
            path=self.db,
        )

        visible = admin_store.list_audit(
            exclude_service_account_activity=True, path=self.db
        )
        self.assertEqual([row["actor_kind"] for row in visible], ["user"])
        self.assertEqual(admin_store.count_service_account_audit(path=self.db), 1)


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

    Tim logs in via OIDC, creates an access plan suitable for his brother,
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

        plan = store_plan("brother-local-only", path=self.db, caps=("chat",))
        accounts.initialize(self.db)
        accounts.change_person(brother["id"], tim["id"], path=self.db, plan_id=plan["id"])

        app = admin_store.ensure_personal_app_for_user(brother["id"], path=self.db)
        self.assertEqual(app["owner_user_id"], brother["id"])
        self.assertEqual(app["access_plan"], "person")
        self.assertEqual(admin_store.person_plan_id(brother["id"], path=self.db), plan["id"])

        secret_hash = _hash("sk_live_brother_real_secret")
        cred = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=app["id"],
            hash=secret_hash,
            prefix="sk_live_brother",
            display_name="brother's editor key",
            path=self.db,
        )
        self.assertEqual(cred["subject_kind"], "app")
        self.assertEqual(cred["app_id"], app["id"])

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

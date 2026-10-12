from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store, service_registry  # noqa: E402
from gateway import admin_runtime  # noqa: E402
from gateway.admin_runtime import runtime_identity_for_app_credential_hash  # noqa: E402
from gateway import accounts  # noqa: E402
from tests.support.access import plan_document, service_app, store_plan  # noqa: E402


class RequestHotpathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.db)

    def tearDown(self) -> None:
        service_registry._normalized_registry_snapshot.cache_clear()
        self.tmp.cleanup()

    def _registry(self, path: Path, label: str) -> dict[str, object]:
        return {
            "services": {
                "service": {
                    "id": "service",
                    "name": label,
                    "source": "ui",
                    "config": {},
                }
            },
            "protocol_frontends": {},
            "workflows": {},
            "capabilities": {},
        }

    def _write_registry(self, path: Path, doc: dict[str, object]) -> None:
        current = admin_store.get_service_registry_with_generation(path=path)
        generation = int(current["operator_settings_generation"]) if current else 0
        admin_store.write_service_registry(
            doc, expected_generation=generation, path=path
        )

    def test_registry_cache_hit_skips_normalization_and_returns_isolated_values(self) -> None:
        self._write_registry(self.db, self._registry(self.db, "Original"))
        service_registry._normalized_registry_snapshot.cache_clear()
        with patch.object(
            service_registry,
            "normalize_service_registry",
            wraps=service_registry.normalize_service_registry,
        ) as normalize:
            first = service_registry.load_service_registry(path=self.db)
            self.assertEqual(normalize.call_count, 1)
            second = service_registry.load_service_registry(path=self.db)
            self.assertEqual(normalize.call_count, 1)

        assert first is not None and second is not None
        first["services"]["service"]["config"]["nested"] = {"items": ["changed"]}
        second = service_registry.load_service_registry(path=self.db)
        assert second is not None
        self.assertNotIn("nested", second["services"]["service"]["config"])

    def test_registry_external_write_invalidates_next_lookup(self) -> None:
        self._write_registry(self.db, self._registry(self.db, "Before"))
        service_registry._normalized_registry_snapshot.cache_clear()
        first = service_registry.load_service_registry(path=self.db)
        self.assertEqual(first["services"]["service"]["name"], "Before")

        self._write_registry(self.db, self._registry(self.db, "After"))
        second = service_registry.load_service_registry(path=self.db)
        self.assertEqual(second["services"]["service"]["name"], "After")

    def test_registry_default_timestamp_is_fresh_on_each_cached_lookup(self) -> None:
        doc = self._registry(self.db, "No timestamp")
        doc["updated_at"] = 0
        self._write_registry(self.db, doc)
        service_registry._normalized_registry_snapshot.cache_clear()

        with patch.object(service_registry.time, "time", side_effect=[10, 11, 12]):
            first = service_registry.load_service_registry(path=self.db)
            second = service_registry.load_service_registry(path=self.db)
        self.assertEqual(first["updated_at"], 11)
        self.assertEqual(second["updated_at"], 12)

    def test_registry_cache_key_separates_databases_at_same_generation(self) -> None:
        other = Path(self.tmp.name) / "other.db"
        admin_store.init_db(other)
        self._write_registry(self.db, self._registry(self.db, "First DB"))
        self._write_registry(other, self._registry(other, "Second DB"))
        service_registry._normalized_registry_snapshot.cache_clear()

        first = service_registry.load_service_registry(path=self.db)
        second = service_registry.load_service_registry(path=other)
        self.assertEqual(first["services"]["service"]["name"], "First DB")
        self.assertEqual(second["services"]["service"]["name"], "Second DB")

    def test_registry_nonmapping_and_invalid_json_keep_existing_behavior(self) -> None:
        self._write_registry(self.db, self._registry(self.db, "Valid"))
        service_registry._normalized_registry_snapshot.cache_clear()
        for encoded in (json.dumps(["not", "a", "mapping"]), "null", "42", "{broken"):
            with self.subTest(encoded=encoded), admin_store._conn(self.db) as conn:
                conn.execute(
                    "UPDATE operator_settings SET value_json = ? WHERE key = 'service_registry'",
                    (encoded,),
                )
                conn.commit()
                with patch.object(service_registry.time, "time", return_value=42):
                    # The previous loader used get_operator_setting, whose
                    # damaged-document fallback remains the behavior oracle.
                    previous = admin_store.get_operator_setting("service_registry", path=self.db)
                    expected = service_registry.normalize_service_registry(previous["value"])
                    self.assertEqual(service_registry.load_service_registry(path=self.db), expected)

    def _credential(self, *, expires_at: int | None = None, plan_id: str | None = None, **plan_kwargs):
        app = service_app("hotpath-app", path=self.db, plan_id=plan_id, **plan_kwargs)
        credential = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=app["id"],
            app_id=app["id"],
            hash=f"hash-{app['id']}",
            prefix="opk_test",
            expires_at=expires_at,
            path=self.db,
        )
        return app, credential

    def test_auth_observes_plan_app_and_credential_changes_on_next_lookup(self) -> None:
        chat = store_plan("chat only", path=self.db, caps=("chat",))
        images = store_plan("images only", path=self.db, caps=("image",))
        app, credential = self._credential(plan_id=chat["id"])
        key_hash = credential["hash"]

        identity = runtime_identity_for_app_credential_hash(key_hash, path=self.db)
        self.assertEqual(set(identity.access.app_plan.can_use), {"chat"})
        admin_store.update_access_plan(chat["id"], plan_document("chat only", caps=("chat", "speech")), path=self.db)
        identity = runtime_identity_for_app_credential_hash(key_hash, path=self.db)
        self.assertEqual(set(identity.access.app_plan.can_use), {"chat", "speech"})

        admin_store.update_app(app["id"], access_plan=images["id"], path=self.db)
        identity = runtime_identity_for_app_credential_hash(key_hash, path=self.db)
        self.assertEqual(identity.access.app_plan.id, images["id"])
        admin_store.update_app(app["id"], access_plan="person", path=self.db)
        identity = runtime_identity_for_app_credential_hash(key_hash, path=self.db)
        self.assertEqual(identity.access.app_plan.id, identity.access.person_plan.id)

        admin_store.disable_app(app["id"], path=self.db)
        self.assertEqual(runtime_identity_for_app_credential_hash(key_hash, path=self.db).denial, "app_disabled")
        admin_store.enable_app(app["id"], path=self.db)
        self.assertIsNone(runtime_identity_for_app_credential_hash(key_hash, path=self.db).denial)
        admin_store.revoke_credential(credential["id"], path=self.db)
        self.assertIsNone(runtime_identity_for_app_credential_hash(key_hash, path=self.db))

    def test_auth_observes_expiry_on_next_lookup(self) -> None:
        _, expires_soon = self._credential(expires_at=101)
        with patch.object(admin_runtime.time, "time", return_value=100):
            self.assertIsNotNone(
                runtime_identity_for_app_credential_hash(expires_soon["hash"], path=self.db)
            )
        with patch.object(admin_runtime.time, "time", return_value=102):
            self.assertIsNone(
                runtime_identity_for_app_credential_hash(expires_soon["hash"], path=self.db)
            )

    def test_auth_observes_the_accountable_persons_plan_and_state_on_next_lookup(self) -> None:
        app, credential = self._credential()
        key_hash = credential["hash"]
        person = admin_store.get_app(app["id"], path=self.db)["owner_user_id"]
        before = runtime_identity_for_app_credential_hash(key_hash, path=self.db)
        self.assertIsNotNone(before.access.person_plan)
        narrower = store_plan("narrower", path=self.db, caps=("chat",))
        accounts.change_person(person, "test", path=self.db, plan_id=narrower["id"])
        after = runtime_identity_for_app_credential_hash(key_hash, path=self.db)
        self.assertEqual(after.access.person_plan.id, narrower["id"])
        # Disabling a person revokes the keys of the Apps they answer for.
        accounts.change_person(person, "test", path=self.db, disabled=True)
        self.assertIsNone(runtime_identity_for_app_credential_hash(key_hash, path=self.db))

    def test_touch_failure_rolls_back_and_later_store_writes_remain_visible(self) -> None:
        app, credential = self._credential()
        def fail_after_write(cred_id: str, *, connection, **kwargs) -> bool:
            connection.execute(
                "UPDATE credentials SET last_used_at = 123 WHERE id = ?", (cred_id,)
            )
            raise RuntimeError("simulated touch failure")

        with patch.object(admin_store, "touch_credential_last_used", side_effect=fail_after_write):
            identity = runtime_identity_for_app_credential_hash(credential["hash"], path=self.db)
        self.assertIsNotNone(identity)
        stored = admin_store.get_credential(credential["id"], path=self.db)
        self.assertIsNone(stored["last_used_at"])

        admin_store.update_app(app["id"], name="visible immediately", path=self.db)
        self.assertEqual(
            runtime_identity_for_app_credential_hash(credential["hash"], path=self.db).name,
            "visible immediately",
        )

    def test_concurrent_resolutions_use_distinct_connections(self) -> None:
        _, credential = self._credential()
        barrier = threading.Barrier(2)
        seen: set[int] = set()
        lock = threading.Lock()
        original_scope = admin_store.runtime_auth_connection

        @contextmanager
        def observed_scope(path=admin_store.DB_PATH):
            with original_scope(path) as connection:
                with lock:
                    seen.add(id(connection))
                barrier.wait(timeout=5)
                yield connection

        with patch.object(admin_store, "runtime_auth_connection", observed_scope), patch.object(
            admin_store, "touch_credential_last_used", return_value=False
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                identities = list(
                    pool.map(
                        lambda _: runtime_identity_for_app_credential_hash(
                            credential["hash"], path=self.db
                        ),
                        range(2),
                    )
                )
        self.assertTrue(all(identities))
        self.assertEqual(len(seen), 2)

    def _traced_resolution(self, key_hash: str) -> tuple[object, list[str]]:
        statements: list[str] = []
        original_scope = admin_store.runtime_auth_connection

        @contextmanager
        def traced_scope(path=admin_store.DB_PATH):
            with original_scope(path) as connection:
                connection.set_trace_callback(statements.append)
                try:
                    yield connection
                finally:
                    connection.set_trace_callback(None)

        with patch.object(admin_store, "runtime_auth_connection", traced_scope), patch.object(
            admin_store, "touch_credential_last_used", return_value=False
        ):
            identity = runtime_identity_for_app_credential_hash(key_hash, path=self.db)
        return identity, [s for s in statements if s.lstrip().upper().startswith("SELECT")]

    def test_session_app_guard_adds_no_query_to_identity_resolution(self) -> None:
        _, credential = self._credential()
        identity, selects = self._traced_resolution(credential["hash"])
        self.assertIsNotNone(identity)
        # One statement reads the credential (with its session-App flag), its
        # App, the accountable person and both plans: one snapshot (design §2.13).
        self.assertEqual(len(selects), 1, selects)

    def test_a_session_app_credential_still_never_authenticates(self) -> None:
        import sqlite3

        user = admin_store.upsert_oidc_user("issuer", "guarded", email="g@example.test", path=self.db)
        app = admin_store.ensure_session_app_for_user(user["id"], name="Azizam", path=self.db)
        conn = sqlite3.connect(self.db)
        try:  # A key written before the guard existed.
            conn.execute("DROP TRIGGER IF EXISTS credentials_never_on_session_apps")
            conn.execute(
                "INSERT INTO credentials(id, subject_kind, subject_id, app_id, hash, prefix, created_at) "
                "VALUES ('cred_legacy', 'app', ?, ?, 'legacy-session-hash', 'opk_legacy', 1)",
                (app["id"], app["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        identity, _ = self._traced_resolution("legacy-session-hash")
        self.assertIsNone(identity)


if __name__ == "__main__":
    unittest.main()

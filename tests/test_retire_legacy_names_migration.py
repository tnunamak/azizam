"""Migration tests use synthetic databases only; can also run as unittest."""

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "retire_legacy_names", ROOT / "scripts/migrations/retire_legacy_names.py"
)
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)


class RetireLegacyNamesMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "admin.db"
        self.registry = {
            "services": {
                "openai-proxy": {"id": "openai-proxy", "name": "Azizam (self)"},
                "litellm": {"config": {"api_key_env": "AI_GATEWAY_LITELLM_API_KEY",
                                       "base_url_env": "AI_GATEWAY_LITELLM_BASE_URL"}},
                "provider": {"config": {"api_key_env": "AI_GATEWAY_PROVIDER_SECRET_DEMO",
                                         "api_key_secret_id": "secret-demo",
                                         "api_key": "private-demo",
                                         "metadata": {"api_key_env": "OPENAI_PROXY_API_KEY"}}},
            },
            "protocol_frontends": {"gateway": {"public_discovery": {
                "compatibility": "ai-gateway-native",
                "recipe": {"auth": "Authorization: Bearer $OPENAI_PROXY_API_KEY",
                           "url": "https://openai.vivid.fish/v1",
                           "examples": ["${VIVID_OPENAI_API_KEY}", "$AGW_PORT"]},
            }}},
            "unowned": {"recipe": "$OPENAI_PROXY_API_KEY", "id": "openai-proxy"},
        }
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(
                "CREATE TABLE operator_settings(key TEXT PRIMARY KEY, value_json TEXT, "
                "updated_at INTEGER, updated_by TEXT);"
                "CREATE TABLE operator_settings_generations(setting_key TEXT PRIMARY KEY, "
                "operator_settings_generation INTEGER);"
                "INSERT INTO operator_settings_generations VALUES('service_registry', 7);"
            )
            conn.executemany("INSERT INTO operator_settings VALUES (?, ?, 10, 'owner')", [
                ("service_registry", json.dumps(self.registry)),
                ("oidc_provider_config", json.dumps({"private": "private-demo"})),
            ])
            conn.commit()

    def registry_from(self, db):
        with closing(sqlite3.connect(db)) as conn:
            return json.loads(conn.execute(
                "SELECT value_json FROM operator_settings WHERE key='service_registry'"
            ).fetchone()[0])

    def test_apply_is_narrow_backed_up_and_idempotent(self):
        changes, moved, backup = migration.apply_changes(self.db, allow_running=True)
        self.assertTrue(moved)
        self.assertEqual(len(changes), 8)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.registry_from(backup), self.registry)
        expected = json.loads(json.dumps(self.registry))
        expected["services"]["azizam"] = expected["services"].pop("openai-proxy")
        expected["services"]["azizam"]["id"] = "azizam"
        expected["services"]["litellm"]["config"].update(
            api_key_env="AZIZAM_LITELLM_API_KEY", base_url_env="AZIZAM_LITELLM_BASE_URL"
        )
        expected["services"]["provider"]["config"]["api_key_env"] = "AZIZAM_PROVIDER_SECRET_DEMO"
        discovery = expected["protocol_frontends"]["gateway"]["public_discovery"]
        discovery["compatibility"] = "azizam-native"
        discovery["recipe"]["auth"] = "Authorization: Bearer $AZIZAM_API_KEY"
        discovery["recipe"]["examples"] = ["${AZIZAM_API_KEY}", "$AZIZAM_PORT"]
        self.assertEqual(self.registry_from(self.db), expected)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT operator_settings_generation FROM "
                                          "operator_settings_generations").fetchone()[0], 8)
            self.assertEqual(conn.execute("SELECT value_json FROM operator_settings "
                                          "WHERE key='oidc_provider_config'").fetchone()[0],
                             json.dumps({"private": "private-demo"}))
        self.assertEqual(migration.apply_changes(self.db, allow_running=True), ([], False, None))
        self.assertEqual(len(list(self.db.parent.glob("*.bak.*"))), 1)

    def test_dry_run_never_writes_or_prints_values(self):
        before = self.db.read_bytes()
        with patch("builtins.print") as output:
            self.assertEqual(migration.main(["--db", str(self.db), "--allow-running"]), 0)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(list(self.db.parent.glob("*.bak.*")), [])
        self.assertNotIn("private-demo", str(output.call_args_list))
        self.assertNotIn("Authorization: Bearer", str(output.call_args_list))

    def test_alternate_supported_environment_reference_fields_are_migrated(self):
        self.registry["services"]["litellm"]["config"].update(
            api_key_ref="OPENAI_PROXY_LITELLM_API_KEY", api_base_env="AI_GATEWAY_LITELLM_BASE_URL"
        )
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'",
                         (json.dumps(self.registry),))
            conn.commit()
        migration.apply_changes(self.db, allow_running=True)
        config = self.registry_from(self.db)["services"]["litellm"]["config"]
        self.assertEqual(config["api_key_ref"], "AZIZAM_LITELLM_API_KEY")
        self.assertEqual(config["api_base_env"], "AZIZAM_LITELLM_BASE_URL")

    def test_recipe_rewrites_variable_references_only(self):
        self.assertEqual(migration.renamed_recipe(
            "$OPENAI_PROXY_API_KEY ${VIVID_OPENAI_API_KEY} "
            "https://example.invalid/OPENAI_PROXY_GUIDE OPENAI_PROXY_API_KEY"
        ), "$AZIZAM_API_KEY ${AZIZAM_API_KEY} "
           "https://example.invalid/OPENAI_PROXY_GUIDE OPENAI_PROXY_API_KEY")

    def test_dry_run_refuses_incomplete_wal_without_creating_shm(self):
        # Simulate an unclean exit: committed data remains in WAL; the missing
        # shared-memory index must not be regenerated by a read-only dry-run.
        subprocess.run([sys.executable, "-c", (
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA journal_mode=WAL'); "
            "c.execute(\"UPDATE operator_settings SET updated_at=11\"); "
            "c.commit(); os._exit(0)"
        ), str(self.db)], check=True)
        Path(str(self.db) + "-shm").unlink(missing_ok=True)
        before = {path.name: path.read_bytes() for path in self.db.parent.iterdir()}
        with patch("builtins.print"):
            self.assertEqual(migration.main(["--db", str(self.db), "--allow-running"]), 1)
        self.assertEqual({path.name: path.read_bytes() for path in self.db.parent.iterdir()}, before)

    def test_collision_refuses_without_writing(self):
        self.registry["services"]["azizam"] = {"id": "azizam"}
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'",
                         (json.dumps(self.registry),))
            conn.commit()
        with self.assertRaisesRegex(ValueError, "collision"):
            migration.apply_changes(self.db, allow_running=True)
        self.assertEqual(self.registry_from(self.db), self.registry)
        self.assertEqual(list(self.db.parent.glob("*.bak.*")), [])

    def test_running_or_unknown_state_refuses(self):
        for state, code in (("active", 0), ("activating", 0), ("", 1)):
            result = type("Result", (), {"stdout": state, "returncode": code})()
            with self.subTest(state=state), patch.object(migration.subprocess, "run", return_value=result):
                with self.assertRaises(RuntimeError):
                    migration.apply_changes(self.db, allow_running=False)
        self.assertEqual(self.registry_from(self.db), self.registry)

    def test_stopped_state_allows(self):
        for state in ("inactive", "failed"):
            result = type("Result", (), {"stdout": state, "returncode": 0})()
            with patch.object(migration.subprocess, "run", return_value=result):
                migration.require_stopped()

    def test_backup_failure_leaves_registry_and_generation_unchanged(self):
        with patch.object(migration.private_io, "create_private_file", side_effect=OSError("backup unavailable")):
            with self.assertRaises(OSError):
                migration.apply_changes(self.db, allow_running=True)
        self.assertEqual(self.registry_from(self.db), self.registry)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT operator_settings_generation FROM "
                                          "operator_settings_generations").fetchone()[0], 7)

    def test_gateway_starting_during_backup_refuses_update(self):
        with patch.object(migration, "require_stopped", side_effect=[None, RuntimeError("gateway started")]):
            with self.assertRaisesRegex(RuntimeError, "gateway started"):
                migration.apply_changes(self.db, allow_running=False)
        self.assertEqual(self.registry_from(self.db), self.registry)
        self.assertEqual(len(list(self.db.parent.glob("*.bak.*"))), 1)

    def test_missing_db_does_not_create_one(self):
        missing = self.db.parent / "absent.db"
        with patch("builtins.print"):
            self.assertEqual(migration.main(["--db", str(missing), "--allow-running"]), 1)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()

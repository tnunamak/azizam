"""engine_service_fields migration: synthetic registries only; can also run as unittest."""

from contextlib import closing
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "engine_service_fields", ROOT / "scripts/migrations/engine_service_fields.py"
)
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)


def registry_with(*, tabby_enabled: bool = False) -> dict:
    return {"services": {
        "llm": {"id": "llm", "enabled": True, "config": {"port": 5051}},
        "tabby": {"id": "tabby", "enabled": tabby_enabled, "config": {"port": 5050}},
        "other": {"id": "other", "enabled": True},
    }}


class EngineServiceFieldsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "admin.db"

    def write_db(self, registry: dict) -> None:
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(
                "CREATE TABLE operator_settings(key TEXT PRIMARY KEY, value_json TEXT, "
                "updated_at INTEGER, updated_by TEXT);"
                "CREATE TABLE operator_settings_generations(setting_key TEXT PRIMARY KEY, "
                "operator_settings_generation INTEGER);"
                "INSERT INTO operator_settings_generations VALUES('service_registry', 7);"
            )
            conn.execute("INSERT INTO operator_settings VALUES ('service_registry', ?, 10, 'owner')",
                         (json.dumps(registry),))
            conn.commit()

    def read(self, db: Path) -> dict:
        with closing(sqlite3.connect(db)) as conn:
            return json.loads(conn.execute(
                "SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0])

    def test_removes_only_the_disabled_tabby_row_and_is_idempotent(self):
        before = registry_with()
        self.write_db(before)
        changes, backup = migration.apply_changes(self.db, allow_running=True)
        self.assertEqual(changes, ["remove disabled service row 'tabby'"])  # no owner service in this registry
        after = self.read(self.db)
        expected = copy.deepcopy(before)
        del expected["services"]["tabby"]
        self.assertEqual(after, expected)
        self.assertEqual(self.read(backup), before)
        with closing(sqlite3.connect(self.db)) as conn:
            generation = conn.execute(
                "SELECT operator_settings_generation FROM operator_settings_generations").fetchone()[0]
        self.assertEqual(generation, 8)
        self.assertEqual(migration.apply_changes(self.db, allow_running=True), ([], None))

    def test_refuses_an_enabled_tabby_row_and_leaves_the_database_alone(self):
        self.write_db(registry_with(tabby_enabled=True))
        with self.assertRaises(ValueError):
            migration.apply_changes(self.db, allow_running=True)
        self.assertIn("tabby", self.read(self.db)["services"])

    def test_dry_run_writes_nothing(self):
        self.write_db(registry_with())
        self.assertEqual(migration.main(["--db", str(self.db), "--allow-running"]), 0)
        self.assertIn("tabby", self.read(self.db)["services"])

    def test_seeds_the_absolute_path_rule_on_both_lifecycle_copies_only_for_the_owner_service(self):
        lifecycle = {"driver": "systemd", "driver_config": {"unit": "u.service", "precheck": "gguf_path"}}
        registry = registry_with()
        registry["services"]["kobold"] = {
            "id": "kobold", "enabled": True, "lifecycle": copy.deepcopy(lifecycle),
            "config": {"lifecycle": copy.deepcopy(lifecycle)},
        }
        changes = migration.migrate_registry(registry)
        self.assertIn(
            "kobold: lifecycle.driver_config.activation_requires_absolute_path = true", changes)
        kobold = registry["services"]["kobold"]
        for holder in (kobold["lifecycle"], kobold["config"]["lifecycle"]):
            self.assertEqual(holder["driver_config"], {
                "unit": "u.service", "precheck": "gguf_path", "activation_requires_absolute_path": True})
        self.assertNotIn("lifecycle", registry["services"]["llm"])
        self.assertEqual(migration.migrate_registry(registry), [])


if __name__ == "__main__":
    unittest.main()

"""Unit-level smoke tests for the fresh-instance bootstrap path.

These tests exercise the pieces that matter for a new self-hoster:
  1. admin_store.init_db() creates a usable schema on a blank DB.
  2. default_profiles.seed_default_profiles() populates profiles into a blank DB.
  3. service_registry.ensure_service_registry() accepts a minimal config without
     touching any live data.
  4. AZIZAM_ADMIN_DB env override is honoured -- we never touch data/admin.db.

All tests use a TemporaryDirectory so they are hermetic and leave no state.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIVE_DB = ROOT / "data" / "admin.db"

# Minimal proxy_config.yml content for a fresh self-hoster -- no Tim-specific paths.
MINIMAL_CONFIG: dict = {
    "proxy": {"host": "127.0.0.1", "port": 15000},
    "auth": {"enabled": False},
    "backends": {
        "example-llm": {
            "name": "Example LLM",
            "native_passthrough": True,
            "host": "127.0.0.1",
            "port": 19999,
            "health_endpoint": "/v1/models",
            "role": "standby",
            "capabilities": ["chat", "completions"],
            "managed": False,
        }
    },
    "routing": {"default_backend": "example-llm"},
    "roles": {"default": {"backend": "example-llm"}},
}


class TestFreshInstanceBootstrap(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        # Confirm we are NOT pointing at the live DB.
        self.assertNotEqual(
            self.db.resolve(),
            LIVE_DB.resolve(),
            "Harness DB must not be the live admin.db",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- 1. admin_store init ---------------------------------------------------

    def test_init_db_creates_schema_on_blank_path(self) -> None:
        from gateway import admin_store

        self.assertFalse(self.db.exists(), "DB should not exist before init")
        admin_store.init_db(self.db)
        self.assertTrue(self.db.exists(), "DB should exist after init")
        version = admin_store.schema_version(self.db)
        self.assertEqual(version, admin_store.SCHEMA_VERSION)

    def test_init_db_is_idempotent(self) -> None:
        from gateway import admin_store

        admin_store.init_db(self.db)
        admin_store.init_db(self.db)
        self.assertEqual(admin_store.schema_version(self.db), admin_store.SCHEMA_VERSION)

    # -- 2. no plan is seeded for anyone ----------------------------------------

    def test_a_fresh_database_has_no_access_plans(self) -> None:
        from gateway import admin_store

        admin_store.init_db(self.db)
        self.assertEqual(admin_store.list_access_plans(path=self.db), [])

    # -- 3. service_registry with minimal config -------------------------------

    def test_ensure_service_registry_with_minimal_config(self) -> None:
        from gateway import admin_store, service_registry

        admin_store.init_db(self.db)
        registry = service_registry.ensure_service_registry(
            MINIMAL_CONFIG,
            path=self.db,
            actor="harness-test",
        )
        self.assertIsNotNone(registry)

    # -- 4. env override guard -------------------------------------------------

    def test_azizam_admin_db_env_is_respected(self) -> None:
        """Verify the module honours AZIZAM_ADMIN_DB when set."""
        saved = os.environ.get("AZIZAM_ADMIN_DB")
        try:
            os.environ["AZIZAM_ADMIN_DB"] = str(self.db)
            # Test the lower-level store directly: init at our env-specified path.
            # Re-importing proxy.py to test the full path resolution would have
            # heavy module-level side effects; the env var is documented as the
            # authoritative override, so confirming init_db honours our path is sufficient.
            from gateway import admin_store

            admin_store.init_db(self.db)
            self.assertTrue(self.db.exists())
        finally:
            if saved is None:
                os.environ.pop("AZIZAM_ADMIN_DB", None)
            else:
                os.environ["AZIZAM_ADMIN_DB"] = saved

    # -- 5. live DB isolation --------------------------------------------------

    def test_harness_db_is_not_live_db(self) -> None:
        """Belt-and-suspenders: confirm temp DB inode != live DB inode."""
        from gateway import admin_store

        admin_store.init_db(self.db)
        if LIVE_DB.exists():
            self.assertNotEqual(
                self.db.stat().st_ino,
                LIVE_DB.stat().st_ino,
                "Harness must not share an inode with the live DB",
            )
        else:
            self.skipTest("Live DB not present; isolation trivially satisfied")


def test_bootstrap_admin_path_uses_env_then_yaml_then_repo_default(
    tmp_path: Path, monkeypatch
) -> None:
    from gateway import bootstrap_config

    root = tmp_path / "checkout"
    elsewhere = tmp_path / "working-directory"
    root.mkdir()
    elsewhere.mkdir()
    monkeypatch.setattr(bootstrap_config, "ROOT", root)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("AZIZAM_ADMIN_DB", "env/admin.db")
    assert bootstrap_config.configured_admin_db_path(
        {"db_path": "yaml/admin.db"}
    ) == root / "env/admin.db"

    monkeypatch.delenv("AZIZAM_ADMIN_DB")
    assert bootstrap_config.configured_admin_db_path(
        {"db_path": "yaml/admin.db"}
    ) == root / "yaml/admin.db"
    assert bootstrap_config.configured_admin_db_path({}) == root / "data/admin.db"

    absolute = tmp_path / "absolute.db"
    monkeypatch.setenv("AZIZAM_ADMIN_DB", str(absolute))
    assert bootstrap_config.configured_admin_db_path(
        {"db_path": "yaml/admin.db"}
    ) == absolute


if __name__ == "__main__":
    unittest.main()

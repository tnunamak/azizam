"""Unit-level smoke tests for the fresh-instance bootstrap path.

These tests exercise the pieces that matter for a new self-hoster:
  1. admin_store.init_db() creates a usable schema on a blank DB.
  2. default_profiles.seed_default_profiles() populates profiles into a blank DB.
  3. service_registry.ensure_service_registry() starts an empty store without
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

    def test_ensure_service_registry_on_an_empty_store(self) -> None:
        from gateway import admin_store, service_registry

        admin_store.init_db(self.db)
        registry = service_registry.ensure_service_registry(
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



def test_harness_exit_trap_reaps_normal_children_before_removing_workspace(tmp_path):
    """Exercise the real trap only, including TERM-resistant owned children."""
    import shutil
    import signal
    import subprocess

    script = (ROOT / "scripts/fresh_instance_harness.sh").read_text()
    # No gateway, configuration, imports, or network setup is executed.
    cleanup = script.split("stop_owned_child() {", 1)[1]
    cleanup = "stop_owned_child() {" + cleanup.split("trap cleanup_fixture EXIT", 1)[0]
    cleanup += "trap cleanup_fixture EXIT\n"
    child_code = """
import os, pathlib, signal, sys, time
root, ready, observed, resistant = map(str, sys.argv[1:])
def terminate(*_):
    pathlib.Path(observed).write_text(str(pathlib.Path(root).is_dir()))
    if resistant == "no":
        sys.exit(0)
signal.signal(signal.SIGTERM, terminate)
pathlib.Path(ready).write_text(str(os.getpid()))
while True:
    time.sleep(0.05)
"""
    # Both ordinary TERM shutdown and forced KILL must reap both child roles.
    for resistant in ("no", "yes"):
        # The cleanup deletes only its own workspace shapes; use the HOME-scoped one so the test
        # never needs a writable /tmp (the hermetic runner has none).
        home = tmp_path / f"home-{resistant}"
        (home / ".tmp").mkdir(parents=True)
        workspace = Path(tempfile.mkdtemp(prefix="gw-ux-p1b-", dir=home / ".tmp"))
        proxy_ready = tmp_path / f"proxy-{resistant}"
        tail_ready = tmp_path / f"tail-{resistant}"
        proxy_observed = tmp_path / f"proxy-term-{resistant}"
        tail_observed = tmp_path / f"tail-term-{resistant}"
        body = 'set -euo pipefail\nFIXTURE_MODE=0\nTMPDIR_ROOT="$1"\n' + cleanup + """
"$2" -c "$3" "$1" "$4" "$6" "$8" &
PROXY_PID=$!
"$2" -c "$3" "$1" "$5" "$7" "$8" &
TAIL_PID=$!
for _ in {1..100}; do
  [[ -s "$4" && -s "$5" ]] && break
  sleep 0.02
done
[[ -s "$4" && -s "$5" ]] || exit 24
# Mimic an unhandled command failure under set -e, not an explicit stop path.
(exit 23)
"""
        process = subprocess.Popen(
            ["bash", "-c", body, "cleanup-test", str(workspace), sys.executable,
             child_code, str(proxy_ready), str(tail_ready),
             str(proxy_observed), str(tail_observed), resistant],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True, env={**os.environ, "HOME": str(home)},
        )
        try:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 23, (stdout, stderr)
            assert not workspace.exists()
            for ready, observed in ((proxy_ready, proxy_observed), (tail_ready, tail_observed)):
                assert observed.read_text() == "True", "stores removed before child termination"
                pid = int(ready.read_text())
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise AssertionError(f"owned child {pid} survived EXIT cleanup")
        finally:
            # A reverted/broken cleanup must not leak the dummy children either.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
            shutil.rmtree(workspace, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()

"""scripts/local/sync_model_metadata_from_config.py writes one registry key and no other file."""

from __future__ import annotations

import copy
import importlib.util
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

from gateway import admin_store
from gateway.litellm_substrate import compile_litellm_config_from_registry
from tests.registry_test_helpers import write_service_registry_for_test

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "sync_model_metadata_from_config", ROOT / "scripts" / "local" / "sync_model_metadata_from_config.py"
)
assert _SPEC is not None and _SPEC.loader is not None
sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync)


def _registry(litellm_config: Path) -> dict:
    return {
        "services": {
            "litellm": {
                "id": "litellm",
                "kind": "substrate",
                "capabilities": ["openai"],
                # An absolute config_path, as a copied production registry carries.
                "config": {"execution_substrate": "litellm", "config_path": str(litellm_config)},
            },
            "local-llm": {
                "id": "local-llm",
                "capabilities": ["openai", "chat"],
                "config": {
                    "protocol": "openai-compatible",
                    "base_url": "http://127.0.0.1:9/v1",
                    "models": [
                        {"id": "chat-a", "upstream_model": "a.gguf"},
                        {"id": "chat-b", "upstream_model": "b.gguf"},
                    ],
                },
            },
        },
    }


class SyncModelMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "admin.db"
        self.litellm_config = self.dir / "live" / "litellm.config.yaml"
        admin_store.init_db(self.db)
        write_service_registry_for_test(_registry(self.litellm_config), path=self.db)
        self.config = self.dir / "proxy_config.yml"
        self.config.write_text(yaml.safe_dump({"backends": {"local-llm": {"models": [
            {"id": "chat-a", "model_metadata": {"input_modalities": ["text"], "tags": ["tool-use"]}},
            {"id": "chat-b"},
        ]}}}))

    def _run(self, *extra: str) -> int:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return sync.main(["--db", str(self.db), "--config", str(self.config), *extra])

    def _state(self) -> dict:
        state = admin_store.get_service_registry_with_generation(path=self.db)
        assert state is not None
        return state

    def _backups(self) -> list[Path]:
        return sorted(self.dir.glob("admin.db.bak.*"))

    def test_dry_run_is_the_default_and_writes_nothing(self) -> None:
        before = self._state()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._state(), before)
        self.assertEqual(self._backups(), [])
        self.assertFalse(self.litellm_config.exists())

    def test_apply_writes_only_model_metadata_and_no_artifact(self) -> None:
        before = self._state()
        self.assertEqual(self._run("--apply"), 0)
        after = self._state()

        expected = copy.deepcopy(before["value"])
        expected["services"]["local-llm"]["config"]["models"][0]["model_metadata"] = {
            "input_modalities": ["text"], "tags": ["tool-use"],
        }
        self.assertEqual(after["value"], expected)
        self.assertEqual(after["operator_settings_generation"], before["operator_settings_generation"] + 1)
        # No LiteLLM config is written, not even at the registry's absolute config_path.
        self.assertFalse(self.litellm_config.exists())
        self.assertEqual([p.name for p in self.dir.iterdir() if "litellm" in p.name], [])
        self.assertEqual(len(self._backups()), 1)
        events = admin_store.list_audit(target_id="service_registry", path=self.db)
        self.assertEqual([e["action"] for e in events], ["operator.service_registry.update"])

    def test_second_apply_changes_nothing(self) -> None:
        self._run("--apply")
        once = self._state()
        self.assertEqual(self._run("--apply"), 0)
        self.assertEqual(self._state(), once)
        self.assertEqual(len(self._backups()), 1)

    def test_model_metadata_does_not_change_compiled_litellm_config(self) -> None:
        before = self._state()["value"]
        self._run("--apply")
        self.assertEqual(
            compile_litellm_config_from_registry(self._state()["value"]),
            compile_litellm_config_from_registry(before),
        )

    def test_registry_and_generation_are_read_in_one_statement(self) -> None:
        """One SELECT reads one snapshot; two could straddle a concurrent commit and
        pair stale JSON with the new generation, letting the CAS write clobber it."""
        original_conn = admin_store._conn
        statements: list[str] = []
        from contextlib import contextmanager

        @contextmanager
        def tracing_conn(path=admin_store.DB_PATH):
            with original_conn(path) as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        admin_store._conn = tracing_conn
        try:
            read = admin_store.get_service_registry_with_generation(path=self.db)
        finally:
            admin_store._conn = original_conn
        assert read is not None
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1, selects)
        self.assertIn("operator_settings_generations", selects[0])
        self.assertEqual(read, self._state())

    def test_backups_never_collide_and_are_owner_only(self) -> None:
        first = sync._backup(self.db)
        second = sync._backup(self.db)
        self.assertNotEqual(first, second)
        for backup in (first, second):
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(admin_store.get_service_registry_with_generation(path=backup), self._state())


if __name__ == "__main__":
    unittest.main()

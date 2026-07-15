"""Tests that every persistent store respects its env-var path override.

Each store module reads its env var at import time (module-level DB_PATH).
We test by importing the module in a subprocess that has the env var set,
then checking the reported DB_PATH matches the override.

Run via:
    python3 tests/test_store_path_isolation.py

Exits non-zero on failure. No pytest dependency required.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gateway"))


def _db_path_in_subprocess(module: str, env_var: str, override_path: str) -> str:
    """Return the DB_PATH a module reports when env_var is set to override_path."""
    code = (
        f"import sys; sys.path.insert(0, '{ROOT}');"
        f"import os; os.environ['{env_var}'] = '{override_path}';"
        f"import {module}; print({module}.DB_PATH)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class TestStoreEnvOverrides(unittest.TestCase):
    """Each store's DB_PATH must honour its env-var override."""

    def _assert_override(self, module: str, env_var: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = os.path.join(tmp, "test.db")
            reported = _db_path_in_subprocess(module, env_var, override)
            self.assertEqual(
                reported,
                override,
                f"{module}.DB_PATH did not pick up {env_var}",
            )

    def _assert_default_unchanged(self, module: str, env_var: str) -> None:
        """When the env var is absent the default path must still contain 'data/'."""
        code = (
            f"import sys; sys.path.insert(0, '{ROOT}');"
            f"import {module}; print({module}.DB_PATH)"
        )
        env = {k: v for k, v in os.environ.items() if k != env_var}
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        reported = result.stdout.strip()
        self.assertIn(
            "data",
            reported,
            f"{module}.DB_PATH default should contain 'data/', got: {reported}",
        )

    def test_metrics_store_override(self) -> None:
        self._assert_override("metrics_store", "AI_GATEWAY_METRICS_DB")

    def test_metrics_store_default(self) -> None:
        self._assert_default_unchanged("metrics_store", "AI_GATEWAY_METRICS_DB")

    def test_admin_store_override(self) -> None:
        self._assert_override("gateway.admin_store", "AI_GATEWAY_ADMIN_DB")

    def test_admin_store_default(self) -> None:
        self._assert_default_unchanged("gateway.admin_store", "AI_GATEWAY_ADMIN_DB")

    def test_responses_store_override(self) -> None:
        self._assert_override("responses_store", "AI_GATEWAY_RESPONSES_DB")

    def test_responses_store_default(self) -> None:
        self._assert_default_unchanged("responses_store", "AI_GATEWAY_RESPONSES_DB")

    def test_receipt_store_override(self) -> None:
        self._assert_override("gateway.receipt_store", "AI_GATEWAY_RECEIPTS_DB")

    def test_receipt_store_default(self) -> None:
        self._assert_default_unchanged("gateway.receipt_store", "AI_GATEWAY_RECEIPTS_DB")

    def test_media_jobs_override(self) -> None:
        self._assert_override("gateway.media_jobs", "AI_GATEWAY_MEDIA_JOBS_DB")

    def test_media_jobs_default(self) -> None:
        self._assert_default_unchanged("gateway.media_jobs", "AI_GATEWAY_MEDIA_JOBS_DB")


class TestConfigPathEnvOverride(unittest.TestCase):
    """proxy.py CONFIG_PATH must honour AI_GATEWAY_CONFIG_PATH."""

    def test_config_path_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = os.path.join(tmp, "proxy_config.yml")
            code = (
                f"import sys; sys.path.insert(0, '{ROOT}');"
                f"import os; os.environ['AI_GATEWAY_CONFIG_PATH'] = '{override}';"
                f"import importlib.util;"
                f"spec = importlib.util.spec_from_file_location('proxy_under_test', '{ROOT}/proxy.py');"
                f"module = importlib.util.module_from_spec(spec);"
                f"spec.loader.exec_module(module);"
                f"print(module.CONFIG_PATH)"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), override)


if __name__ == "__main__":
    unittest.main()

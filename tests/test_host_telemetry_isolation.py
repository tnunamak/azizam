"""Host disk/thermal telemetry must never leak into an isolated instance.

status_endpoint's `_disk_partitions`/`_read_sensors` shell out to `df`/`sensors`
against the *runner's own machine*. `AZIZAM_HOST_TELEMETRY_DISABLED` is the
isolation seam any isolated launch mode (harness, dev, tests) sets so a fresh
instance never reports the developer's own disk/thermal state as if it were
the gateway's own host health.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class HostTelemetryIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_env = {
            k: os.environ.get(k)
            for k in ("AZIZAM_HOST_TELEMETRY_DISABLED", "AZIZAM_DISK_PARTITIONS")
        }
        import status_endpoint

        # reload() rebinds the shared module in place; restore its entry
        # bindings (not a fresh reload) so later tests see the same functions.
        entry_bindings = vars(status_endpoint).copy()
        self.addCleanup(self._restore_module, status_endpoint, entry_bindings)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, value in self._orig_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _restore_module(module, bindings: dict) -> None:
        vars(module).clear()
        vars(module).update(bindings)

    def _reload_with_env(self, **env: str):
        for key in ("AZIZAM_HOST_TELEMETRY_DISABLED", "AZIZAM_DISK_PARTITIONS"):
            os.environ.pop(key, None)
        os.environ.update(env)
        import status_endpoint

        return importlib.reload(status_endpoint)

    def test_disabled_flag_skips_disk_shell_out(self) -> None:
        mod = self._reload_with_env(AZIZAM_HOST_TELEMETRY_DISABLED="1")
        calls: list[list[str]] = []
        mod._run = lambda cmd, timeout=None: (calls.append(cmd) or (1, "", ""))  # type: ignore[assignment]
        self.assertEqual(mod._disk_partitions(), [])
        self.assertEqual(calls, [], "disabled flag must skip the df shell-out entirely")

    def test_disabled_flag_skips_sensors_shell_out(self) -> None:
        mod = self._reload_with_env(AZIZAM_HOST_TELEMETRY_DISABLED="1")
        calls: list[list[str]] = []
        mod._run = lambda cmd, timeout=None: (calls.append(cmd) or (1, "", ""))  # type: ignore[assignment]
        result = mod._read_sensors()
        self.assertEqual(calls, [], "disabled flag must skip the sensors shell-out entirely")
        self.assertIn("unavailable_reason", result)

    def test_disk_partitions_default_to_root_only(self) -> None:
        mod = self._reload_with_env()
        self.assertEqual(mod._DISK_PARTITIONS_CONFIG, ["/"])

    def test_disk_partitions_config_is_operator_declared(self) -> None:
        mod = self._reload_with_env(AZIZAM_DISK_PARTITIONS="/, /data")
        self.assertEqual(mod._DISK_PARTITIONS_CONFIG, ["/", "/data"])

    def test_no_hardcoded_operator_specific_mount_literal(self) -> None:
        # Regression guard for the AGENTS.md binding principle: no
        # maintainer-machine-specific path literal in core status code.
        source = (ROOT / "status_endpoint.py").read_text(encoding="utf-8")
        self.assertNotIn("/media/windows", source)


if __name__ == "__main__":
    unittest.main()

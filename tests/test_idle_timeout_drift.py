"""Tests for ``scripts/local/check_idle_timeout_drift.py``."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.local.check_idle_timeout_drift import check_drift, main  # noqa: E402


_START_SH_TEMPLATE = (
    "#!/bin/bash\n"
    'export TABBY_IDLE_TIMEOUT="${{TABBY_IDLE_TIMEOUT:-{value}}}"\n'
)

_SERVICE_TEMPLATE = (
    "[Service]\n"
    'Environment="TABBY_IDLE_TIMEOUT={value}"\n'
)


class TestCheckDrift(unittest.TestCase):
    def _make_pair(self, shell_value: str, service_value: str) -> tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp())
        start_sh = tmp / "start.sh"
        unit = tmp / "azizam.service"
        start_sh.write_text(_START_SH_TEMPLATE.format(value=shell_value))
        unit.write_text(_SERVICE_TEMPLATE.format(value=service_value))
        return start_sh, unit

    def test_no_drift_when_values_match(self) -> None:
        start_sh, unit = self._make_pair("1800", "1800")
        shell, svc, drift = check_drift(start_sh, unit)
        self.assertEqual(shell, "1800")
        self.assertEqual(svc, "1800")
        self.assertFalse(drift)

    def test_drift_detected_when_values_differ(self) -> None:
        start_sh, unit = self._make_pair("1800", "300")
        shell, svc, drift = check_drift(start_sh, unit)
        self.assertEqual(shell, "1800")
        self.assertEqual(svc, "300")
        self.assertTrue(drift)

    def test_missing_file_returns_none_no_crash(self) -> None:
        start_sh, unit = self._make_pair("1800", "300")
        unit.unlink()
        shell, svc, drift = check_drift(start_sh, unit)
        self.assertEqual(shell, "1800")
        self.assertIsNone(svc)
        self.assertFalse(drift)

    def test_main_exits_nonzero_on_drift(self) -> None:
        start_sh, unit = self._make_pair("1800", "300")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(
                [
                    "--start-sh",
                    str(start_sh),
                    "--service-unit",
                    str(unit),
                ]
            )
        out = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("drift", out.lower())
        self.assertIn("1800", out)
        self.assertIn("300", out)

    def test_main_exits_zero_when_aligned(self) -> None:
        start_sh, unit = self._make_pair("1800", "1800")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(
                [
                    "--start-sh",
                    str(start_sh),
                    "--service-unit",
                    str(unit),
                ]
            )
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("ok", out.lower())

    def test_main_does_not_modify_unit_file(self) -> None:
        start_sh, unit = self._make_pair("1800", "300")
        before = unit.read_text()
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--start-sh", str(start_sh), "--service-unit", str(unit)])
        self.assertEqual(unit.read_text(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

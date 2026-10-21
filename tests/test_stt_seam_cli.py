"""The standalone seam check must not assume an engine identity."""

import subprocess
import sys
from pathlib import Path


def test_model_is_required_before_running_a_seam_check(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "stt-seam-check"
    result = subprocess.run(
        [sys.executable, str(script), "--recordings", "missing/recording", "--arm", f"test={tmp_path}",
         "--out", str(tmp_path / "out"), "--allow-busy-gpu"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "--model" in result.stderr and "required" in result.stderr.lower()

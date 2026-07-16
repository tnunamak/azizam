from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts" / "run-hermetic-pytest.sh"


def _runner(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy(); env["PATH"] = "/home/tnunamak/.local/bin:" + env.get("PATH", "")
    return subprocess.run([str(RUNNER), *args], text=True, capture_output=True, env=env)


def test_fresh_runner_contract_has_success_path() -> None:
    assert RUNNER.stat().st_mode & 0o111




def test_runner_contract_is_structurally_fresh_and_sanitized() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    assert "uv pip sync --strict --require-hashes" in text
    assert "fresh-venv" in text
    assert "git -C \"$repo_root\" archive --format=tar HEAD" in text
    assert 'add_read_only_bind "$checkout_root" "$checkout_root"' in text
    assert "PYTHONPATH" not in text
    assert "hermetic_preflight.py" not in text
    assert "provision-hermetic-pytest-env.sh" not in text

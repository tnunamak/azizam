from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts" / "run-hermetic-pytest.sh"


def test_fresh_runner_baseline_and_cleanup() -> None:
    before = {p for p in (Path.home() / ".tmp").glob("vivid-fish-pytest.*")}
    result = subprocess.run([str(RUNNER), "--check"], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    after = {p for p in (Path.home() / ".tmp").glob("vivid-fish-pytest.*")}
    assert after == before


def test_runner_rejects_dirty_and_untracked_inputs_before_provisioning(tmp_path: Path) -> None:
    probe = ROOT / "tests" / ".hermetic-contract-probe"
    try:
        probe.write_text("untracked\n")
        result = subprocess.run([str(RUNNER), "--check"], text=True, capture_output=True)
        assert result.returncode == 2
        assert "untracked checkout input" in result.stderr
    finally:
        probe.unlink(missing_ok=True)


def test_runner_rejects_manifest_lock_mismatch_before_provisioning() -> None:
    manifest = ROOT / "requirements.txt"
    original = manifest.read_bytes()
    try:
        manifest.write_bytes(original + b"\n# mismatch probe\n")
        result = subprocess.run([str(RUNNER), "--check"], text=True, capture_output=True)
        assert result.returncode == 2
        assert "clean reviewed checkout" in result.stderr
    finally:
        manifest.write_bytes(original)


def test_runner_contract_is_structurally_fresh_and_sanitized() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    assert "uv pip sync --strict --require-hashes" in text
    assert "fresh-venv" in text
    assert "git archive --format=tar HEAD" in text
    assert "--ro-bind \"$checkout_root\" \"$checkout_root\"" in text
    assert "PYTHONPATH" not in text
    assert "hermetic_preflight.py" not in text
    assert "provision-hermetic-pytest-env.sh" not in text

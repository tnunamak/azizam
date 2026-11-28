"""Entry points that run heavy suites must end at nice 19 and idle I/O class."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = ["scripts/ci-local", "scripts/run-hermetic-pytest.sh", "scripts/test-affected"]


def _run(argv: list[str], **env: str) -> str:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("AZIZAM_")}
    clean.update(AZIZAM_PRIORITY_DRYRUN="1", **env)
    done = subprocess.run(argv, cwd=ROOT, env=clean, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_entry_point_drops_to_idle_priority(entry: str) -> None:
    out = _run([str(ROOT / entry), "--help"])
    assert re.fullmatch(r"nice=19 ionice=idle", out), out


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_escape_hatch_keeps_normal_priority(entry: str) -> None:
    out = _run([str(ROOT / entry), "--help"], AZIZAM_TEST_PRIORITY="normal")
    assert out.startswith(f"nice={os.nice(0)} "), out


def test_already_niced_process_is_not_reniced_past_19() -> None:
    out = _run(["nice", "-n", "19", str(ROOT / "scripts/low-priority"), "true"])
    assert out.startswith("nice=19 ionice=idle"), out


def test_web_heavy_scripts_use_the_helper() -> None:
    scripts = json.loads((ROOT / "web/package.json").read_text())["scripts"]
    for name in ("test:components", "test:e2e"):
        assert scripts[name].startswith("../scripts/low-priority "), name
    assert "idle" in _run([str(ROOT / "scripts/low-priority"), "vitest"])

@pytest.mark.parametrize("change", ["delete", "rename", "edit"])
def test_affected_python_module_removal_selects_full_suite(tmp_path: Path, change: str) -> None:
    """Exercise the real CLI diff/selection path, without executing pytest."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "test-affected"
    script.write_text((ROOT / "scripts/test-affected").read_text())
    package = tmp_path / "sample_package"
    package.mkdir()
    (package / "__init__.py").write_text("")
    module = package / "feature.py"
    module.write_text("VALUE = 1\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_importer.py").write_text("import sample_package.feature\n")
    (tests / "test_unrelated.py").write_text("def test_unrelated(): pass\n")
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("AZIZAM_", "GIT_"))}
    clean["AZIZAM_PRIORITY_SET"] = "1"

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, env=clean, check=True,
                       capture_output=True, text=True, timeout=30)

    git("init")
    git("config", "user.name", "Selection Test")
    git("config", "user.email", "selection@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("add", ".")
    git("commit", "-m", "base")
    git("tag", "selection-base")
    if change == "delete":
        module.unlink()
    elif change == "rename":
        module.rename(package / "replacement.py")
    else:
        module.write_text("VALUE = 2\n")
    git("add", "-A")
    git("commit", "-m", change)
    done = subprocess.run(
        [sys.executable, str(script), "--base", "selection-base", "--list"],
        cwd=tmp_path, env=clean, capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stderr
    if change in {"delete", "rename"}:
        assert "python: deleted or renamed-away Python file; selecting the whole suite" in done.stdout
        assert "python: 0 test module(s)" not in done.stdout
    else:
        assert "python: 1 test module(s)" in done.stdout
        assert "  tests/test_importer.py" in done.stdout
        assert "tests/test_unrelated.py" not in done.stdout
        assert "selecting the whole suite" not in done.stdout

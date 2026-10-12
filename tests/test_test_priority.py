"""Entry points that run heavy suites must end at nice 19 and idle I/O class."""

from __future__ import annotations

import json
import os
import re
import subprocess
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

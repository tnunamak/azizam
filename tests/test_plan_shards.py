"""scripts/e2e/plan_shards.py: a seed varies lane assignment, never coverage."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PLAN = Path(__file__).resolve().parents[1] / "scripts" / "e2e" / "plan_shards.py"
FILES = [f"f{index}.spec.ts" for index in range(9)]


def _plan(tmp_path: Path, name: str, *seed: str) -> list[str]:
    listing = tmp_path / "list.txt"
    listing.write_text("".join(f"  [chromium] › {file}:3:1 › test\n" for file in FILES))
    out = tmp_path / name
    subprocess.run([sys.executable, str(PLAN), "plan", str(listing), str(tmp_path / "no-weights.tsv"), "3",
                    str(out), *seed], check=True, capture_output=True, text=True)
    return [(out / f"lane-{lane}.txt").read_text() for lane in (1, 2, 3)]


def test_seed_reproduces_its_plan_and_keeps_every_file_once(tmp_path: Path) -> None:
    plans = {seed: _plan(tmp_path, f"seed-{seed}", str(seed)) for seed in range(8)}
    assert _plan(tmp_path, "again", "3") == plans[3]
    assert len({tuple(plan) for plan in plans.values()}) > 1
    for plan in [*plans.values(), _plan(tmp_path, "unseeded")]:
        files = "".join(plan).split()
        assert sorted(files) == sorted(FILES)
        assert [len(lane.split()) for lane in plan] == [3, 3, 3]  # equal files: the slack keeps balance

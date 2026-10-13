"""scripts/ci/prune-receipts.sh keeps the gate's receipt directory bounded (TEST_STRATEGY.md 1.3)."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "prune-receipts.sh"
DAY = 86400


def _receipt(root: Path, name: str, age_s: float, artifact_kib: int = 4) -> Path:
    receipt = root / name
    (receipt / "test-results").mkdir(parents=True)
    (receipt / "cases.jsonl").write_text("{}\n")
    (receipt / "test-results" / "trace.zip").write_bytes(b"x" * artifact_kib * 1024)
    stamp = time.time() - age_s
    os.utime(receipt, (stamp, stamp))
    return receipt


def _prune(root: Path, *args: str, **env: str) -> None:
    subprocess.run(["bash", str(SCRIPT), str(root), *args], check=True, env={**os.environ, **env})


def test_only_the_newest_runs_keep_their_browser_artifacts(tmp_path: Path) -> None:
    receipts = [_receipt(tmp_path, f"r{i}", age_s=i * 60) for i in range(8)]
    _prune(tmp_path)
    assert [r.name for r in receipts if (r / "test-results").exists()] == ["r0", "r1", "r2", "r3", "r4"]
    assert all((r / "cases.jsonl").exists() for r in receipts)


def test_the_run_that_just_ended_is_never_pruned(tmp_path: Path) -> None:
    receipts = [_receipt(tmp_path, f"r{i}", age_s=i * 60) for i in range(7)]
    _prune(tmp_path, "--keep", str(receipts[6]), AZIZAM_RECEIPTS_KEEP_ARTIFACTS="1")
    assert (receipts[6] / "test-results").exists()
    assert not (receipts[5] / "test-results").exists()


def test_receipts_past_the_age_limit_are_removed(tmp_path: Path) -> None:
    fresh = _receipt(tmp_path, "fresh", age_s=DAY)
    old = _receipt(tmp_path, "old", age_s=91 * DAY)
    _prune(tmp_path)
    assert fresh.exists() and not old.exists()


def test_the_oldest_receipts_go_first_until_the_total_fits(tmp_path: Path) -> None:
    keep = _receipt(tmp_path, "current", age_s=0)
    older = [_receipt(tmp_path, f"r{i}", age_s=(i + 1) * 60) for i in range(3)]
    _prune(tmp_path, "--keep", str(keep), AZIZAM_RECEIPTS_MAX_GIB="0")
    assert keep.exists() and not any(r.exists() for r in older)

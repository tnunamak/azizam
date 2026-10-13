"""Test harnesses delete their own scratch directories (scripts/lib/scratch.sh, scripts/scratch_owner.py)
and receipts keep a bounded size (scripts/ci/prune_receipts.py).

A harness removes its directory on EXIT, HUP, INT and TERM. SIGKILL runs no trap, so the next start
removes directories whose owner (pid and start time in .scratch-owner) is dead. A directory with a live
owner, no marker or a symlink is left alone, and so is the one path named in SCRATCH_KEEP.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib" / "scratch.sh"
PRUNE = ROOT / "scripts" / "ci" / "prune_receipts.py"
sys.path.insert(0, str(ROOT / "scripts"))
import scratch_owner  # noqa: E402

# A fake harness: sweep, make a marked directory, announce it, wait for a signal.
HARNESS = f"""
source {LIB}
scratch_sweep "$1" fake-
dir=$(mktemp -d "$1/fake-XXXXXX")
scratch_mark "$dir"
trap 'rm -rf -- "$dir"' EXIT
scratch_on_signal
echo "$dir" > "$1/announced"
sleep 60 &
wait $!
"""


def _start_harness(root: Path) -> tuple[subprocess.Popen[bytes], Path]:
    process = subprocess.Popen(["bash", "-c", HARNESS, "harness", str(root)])
    deadline = time.monotonic() + 10
    while not (root / "announced").exists():
        assert time.monotonic() < deadline and process.poll() is None, "harness did not start"
        time.sleep(0.02)
    return process, Path((root / "announced").read_text().strip())


def _dead_pid() -> tuple[int, str]:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    started = scratch_owner.start_time(dead.pid)
    dead.wait()
    return dead.pid, str(started)


def _plant(root: Path, name: str, owner: str | None) -> Path:
    path = root / name
    (path / "data").mkdir(parents=True)
    (path / "data" / "copy-of-owner-data").write_text("x")
    if owner is not None:
        (path / ".scratch-owner").write_text(owner + "\n")
    return path


@pytest.mark.parametrize("signal_name", ["TERM", "HUP"])  # INT: xdist workers start with SIGINT ignored, which bash cannot trap
def test_a_signalled_harness_leaves_nothing(tmp_path: Path, signal_name: str) -> None:
    process, directory = _start_harness(tmp_path)
    assert directory.is_dir()
    subprocess.run(["kill", f"-{signal_name}", str(process.pid)], check=True)
    process.wait(timeout=20)
    assert not directory.exists()
    assert process.returncode == {"TERM": 143, "HUP": 129}[signal_name]


def test_a_dead_owners_directory_is_removed_when_the_next_harness_starts(tmp_path: Path) -> None:
    pid, started = _dead_pid()
    stale = _plant(tmp_path, "fake-stale", f"{pid}-{started}")
    reused_pid = _plant(tmp_path, "fake-reused", f"{os.getpid()}-{int(scratch_owner.start_time(os.getpid())) + 1}")
    live = _plant(tmp_path, "fake-live", f"{os.getpid()}-{scratch_owner.start_time(os.getpid())}")
    unmarked = _plant(tmp_path, "fake-unmarked", None)
    other = _plant(tmp_path, "other-stale", f"{pid}-{started}")
    link = tmp_path / "fake-link"
    link.symlink_to(other)
    process, directory = _start_harness(tmp_path)
    try:
        assert not stale.exists() and not reused_pid.exists()
        assert live.is_dir() and unmarked.is_dir() and other.is_dir() and link.is_symlink()
        assert directory.is_dir()
    finally:
        subprocess.run(["kill", "-TERM", str(process.pid)], check=True)
        process.wait(timeout=20)


def test_the_kept_path_survives_the_sweep(tmp_path: Path) -> None:
    pid, started = _dead_pid()
    kept = _plant(tmp_path, "fake-kept", f"{pid}-{started}")
    swept = _plant(tmp_path, "fake-swept", f"{pid}-{started}")
    script = f'source {LIB}; SCRATCH_KEEP={kept} scratch_sweep {tmp_path} fake-'
    subprocess.run(["bash", "-c", script], check=True)
    assert kept.is_dir() and not swept.exists()


def test_the_python_twin_sweeps_and_unwinds_on_term(tmp_path: Path) -> None:
    pid, started = _dead_pid()
    stale = _plant(tmp_path, "fake-stale", f"{pid}-{started}")
    kept = _plant(tmp_path, "fake-kept", f"{pid}-{started}")
    assert scratch_owner.sweep(tmp_path, "fake-", keep=kept) == [stale]
    assert kept.is_dir()
    program = f"""
import sys, tempfile, time
from pathlib import Path
sys.path.insert(0, {str(ROOT / "scripts")!r})
import scratch_owner
scratch_owner.exit_on_signals()
with tempfile.TemporaryDirectory(prefix="fake-", dir={str(tmp_path)!r}) as d:
    scratch_owner.mark(Path(d))
    Path({str(tmp_path / "announced")!r}).write_text(d)
    time.sleep(60)
"""
    process = subprocess.Popen([sys.executable, "-c", program])
    deadline = time.monotonic() + 10
    while not (tmp_path / "announced").exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    directory = Path((tmp_path / "announced").read_text())
    assert directory.is_dir()
    subprocess.run(["kill", "-TERM", str(process.pid)], check=True)
    process.wait(timeout=20)
    assert not directory.exists()


# -- receipts ------------------------------------------------------------------

def _receipt(root: Path, name: str, age_days: float, signoff_age_days: float | None = None) -> Path:
    run = root / name
    (run / "logs").mkdir(parents=True)
    (run / "test-results").mkdir()
    (run / "cases.jsonl").write_text("x" * 100)
    (run / "logs" / "a.log").write_text("log")
    (run / "run.tsv").write_text("sha\tx\n")
    (run / "PASS").write_text("x")
    stamps = {run / "run.tsv": age_days}
    if signoff_age_days is not None:
        (run / f"signoff-{name}.tsv").write_text("posted\n")
        stamps[run / f"signoff-{name}.tsv"] = signoff_age_days
    now = time.time()
    for path, days in stamps.items():
        os.utime(path, (now - days * 86400, now - days * 86400))
    return run


def _prune(root: Path, *extra: str) -> None:
    subprocess.run([sys.executable, str(PRUNE), str(root), *extra], check=True, capture_output=True)


def test_old_receipts_keep_only_their_verdicts(tmp_path: Path) -> None:
    runs = [_receipt(tmp_path, f"r{index:02d}", age_days=index / 10) for index in range(25)]  # r00 is newest
    recent_signoff = _receipt(tmp_path, "signed-old", age_days=30, signoff_age_days=2)
    stale_signoff = _receipt(tmp_path, "signed-stale", age_days=30, signoff_age_days=9)
    scheduled = tmp_path / "scheduled" / "nightly-1"
    scheduled.mkdir(parents=True)
    (scheduled / "receipt.json").write_text("{}")
    _prune(tmp_path, "--keep", str(runs[-1]))
    for run in runs[:20] + [recent_signoff, runs[-1]]:
        assert (run / "cases.jsonl").is_file() and (run / "logs" / "a.log").is_file(), run.name
    for run in runs[20:-1]:
        assert sorted(path.name for path in run.iterdir()) == ["PASS", "run.tsv"], run.name
    assert sorted(path.name for path in stale_signoff.iterdir()) == ["PASS", "run.tsv", "signoff-signed-stale.tsv"]
    assert (scheduled / "receipt.json").is_file()


def test_pruning_a_missing_or_empty_root_is_harmless(tmp_path: Path) -> None:
    _prune(tmp_path / "absent")
    _prune(tmp_path)

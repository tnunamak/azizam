"""A SIGKILLed hermetic runner leaves its /dev/shm allocation behind; the next run sweeps it
(scripts/sweep-stale-pytest-tmpfs.sh) without touching a live run's allocation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SWEEP = Path(__file__).resolve().parents[1] / "scripts" / "sweep-stale-pytest-tmpfs.sh"


def _start_time(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]


def _live_owner() -> subprocess.Popen[bytes]:
    """A process that lives until its stdin closes (the hermetic run blocks signals)."""
    return subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)


def _allocation(root: Path, pid: int, started: str, suffix: str = "AbCd1234") -> Path:
    path = root / f"azizam-pytest.{pid}-{started}.{suffix}"
    (path / "pytest-of-root").mkdir(parents=True)
    (path / "pytest-of-root" / "data").write_bytes(b"x")
    return path


def _sweep(root: Path) -> None:
    subprocess.run([str(SWEEP), str(root)], check=True, capture_output=True)


def test_a_dead_owners_allocation_is_removed_and_a_live_owners_is_kept(tmp_path: Path) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_started = _start_time(dead.pid)
    dead.wait()
    live = _live_owner()
    try:
        stale = _allocation(tmp_path, dead.pid, dead_started)
        running = _allocation(tmp_path, live.pid, _start_time(live.pid))
        _sweep(tmp_path)
        assert not stale.exists()
        assert (running / "pytest-of-root" / "data").read_bytes() == b"x"
    finally:
        live.stdin.close()
        live.wait()


def test_a_reused_pid_with_a_different_start_time_is_a_dead_owner(tmp_path: Path) -> None:
    live = _live_owner()
    try:
        stale = _allocation(tmp_path, live.pid, str(int(_start_time(live.pid)) + 1))
        _sweep(tmp_path)
        assert not stale.exists()
    finally:
        live.stdin.close()
        live.wait()


def test_names_without_an_owner_and_other_files_are_left_alone(tmp_path: Path) -> None:
    unowned = tmp_path / "azizam-pytest.AbCd1234"
    unowned.mkdir()
    other = tmp_path / "somebody-elses"
    other.mkdir()
    link = tmp_path / "azizam-pytest.1-1.AbCd1234"
    link.symlink_to(other)
    _sweep(tmp_path)
    assert unowned.is_dir() and other.is_dir() and link.is_symlink()

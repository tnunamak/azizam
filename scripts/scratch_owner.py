"""Scratch-directory ownership for Python test harnesses (the twin of scripts/lib/scratch.sh).

A harness deletes its scratch directory when it ends, and a marker file names its owner,
"<pid>-<start time>", so the next start can remove what a SIGKILLed harness left. A reused PID
has a different start time and does not keep a dead owner's directory alive.
"""

from __future__ import annotations

import os
import shutil
import signal
from pathlib import Path

MARKER = ".scratch-owner"


def start_time(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def mark(directory: Path) -> None:
    """Record the calling process as the owner of DIRECTORY."""
    (directory / MARKER).write_text(f"{os.getpid()}-{start_time(os.getpid())}\n")


def owner_alive(directory: Path) -> bool:
    try:
        pid, _, started = (directory / MARKER).read_text().strip().partition("-")
    except FileNotFoundError:
        return True  # no marker: not ours to judge
    except OSError:
        return False
    return pid.isdigit() and start_time(int(pid)) == started


def sweep(root: Path, prefix: str, keep: Path | None = None) -> list[Path]:
    """Remove ROOT/PREFIX* directories whose owner is dead; return what was removed."""
    removed = []
    for directory in sorted(root.glob(f"{prefix}*")):
        if directory.is_symlink() or not directory.is_dir() or directory == keep:
            continue
        if directory.stat().st_uid == os.getuid() and not owner_alive(directory):
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(directory)
    return removed


def exit_on_signals() -> None:
    """Turn TERM, INT and HUP into SystemExit so `finally` blocks and context managers run."""
    def stop(signum, _frame):
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signal.signal(getattr(signal, name), stop)

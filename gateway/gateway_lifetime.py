"""The lifetime lock a running gateway holds on its database (owner visibility step 2, design §8.1).

A gateway takes ``admin.db.running`` shared when it starts and keeps it until it exits. The file sits beside the
database, not in the private directory, so an operator shell that does not carry the gateway's environment still
sees the lock. The App-binding cut-over (gateway/app_binding_cutover.py) takes the same file exclusively, and
refuses when it cannot.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO

_held: IO[bytes] | None = None  # the open file keeps the shared lock for the life of the process


def lock_path(admin_db: Path) -> Path:
    real = Path(os.path.realpath(admin_db))
    return real.with_name(f"{real.name}.running")


def open_lock_file(admin_db: Path) -> IO[bytes]:
    """The lock file, created private on first use and never followed through a link."""
    return os.fdopen(os.open(lock_path(admin_db), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+b")


def hold(admin_db: Path) -> None:
    """Keep the shared lock until this process exits. Called once, at gateway start.

    It waits while the cut-over holds the file exclusively, so a gateway that starts during apply starts after it.
    """
    global _held
    if _held is not None:
        return
    handle = open_lock_file(admin_db)
    fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
    _held = handle

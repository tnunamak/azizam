"""The lifetime lock a running gateway holds on its database (owner visibility step 2, design §8.1).

A gateway takes ``admin.db.running`` shared for as long as its lifespan runs (``proxy.lifespan``), whichever launcher
started it. The file sits beside the database, not in the private directory, so an operator shell that does not carry
the gateway's environment still sees the lock. The App-binding cut-over (gateway/app_binding_cutover.py) takes the same
file exclusively, and refuses when it cannot.
"""

from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path
from typing import IO

_mutex = threading.Lock()
_files: dict[Path, IO[bytes]] = {}  # per database this process holds: the open file that keeps the shared lock
_holds: dict[Path, int] = {}        # per database: how many holds this process has; the last release drops the lock


def lock_path(admin_db: Path) -> Path:
    real = Path(os.path.realpath(admin_db))
    return real.with_name(f"{real.name}.running")


def open_lock_file(admin_db: Path) -> IO[bytes]:
    """The lock file, created private on first use and never followed through a link."""
    return os.fdopen(os.open(lock_path(admin_db), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+b")


def hold(admin_db: Path) -> None:
    """Take the shared lock for this process. Once per database: every further hold only counts.

    It waits while the cut-over holds the file exclusively, so a gateway that starts during apply starts after it.
    Each hold is paired with a ``release``.
    """
    key = Path(os.path.realpath(admin_db))
    with _mutex:
        if key not in _files:
            handle = open_lock_file(admin_db)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            except BaseException:
                handle.close()
                raise
            _files[key] = handle
        _holds[key] = _holds.get(key, 0) + 1


def release(admin_db: Path) -> None:
    """Drop one hold. The shared lock goes when the last hold this process has on the database is dropped."""
    key = Path(os.path.realpath(admin_db))
    with _mutex:
        count = _holds.get(key, 0)
        if count == 0:
            return
        if count > 1:
            _holds[key] = count - 1
            return
        del _holds[key]
        _files.pop(key).close()

"""Check existing SQLite state without mistaking contention for corruption.

Each store has a ``<database>.lock`` file. Every connection opened through
``connect`` or ``connect_readonly`` holds a shared ``flock`` on it for its
lifetime. Startup validation takes the exclusive lock first and holds it
through quarantine and the caller's initialization, so no admitted connection
can open, recreate or write the files while they move. Startup quarantines
only while it holds the exclusive lock; when another admitted connection is
open it checks under a shared lock and refuses to move a corrupt store.
Connections that bypass these helpers (for example the ``sqlite3`` shell) are
not excluded; stop them before recovery.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from gateway import private_io

# Lock files this thread already holds; nested connects must not block on them.
_held = threading.local()


def is_corruption(error: sqlite3.DatabaseError) -> bool:
    code = getattr(error, "sqlite_errorcode", 0)
    return (code & 0xFF) in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} or any(
        text in str(error).lower()
        for text in ("file is not a database", "database disk image is malformed", "malformed database schema")
    )


def lock_path(path: Path | str) -> Path:
    resolved = Path(path).resolve()
    return resolved.with_name(f"{resolved.name}.lock")


def _held_locks() -> set[Path]:
    if not hasattr(_held, "paths"):
        _held.paths = set()
    return _held.paths


def _open_lock(path: Path | str):
    """Open the store lock file; never delete it, or two holders could lock different files."""
    fd = os.open(lock_path(path), os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "r+b")


class _LockedConnection(sqlite3.Connection):
    """Releases the shared store lock when closed or garbage-collected."""

    _store_lock: Any = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._store_lock is not None:
                self._store_lock.close()
                self._store_lock = None


def _admit(path: Path, target: str, uri: bool, kwargs: dict[str, Any]) -> sqlite3.Connection:
    if lock_path(path) in _held_locks():
        check_quarantine_pending(path)
        return sqlite3.connect(target, uri=uri, **kwargs)
    try:
        lock = _open_lock(path)
    except FileNotFoundError:
        # No directory, so no database to protect; let SQLite report the open error.
        return sqlite3.connect(target, uri=uri, **kwargs)
    try:
        fcntl.flock(lock, fcntl.LOCK_SH)
        check_quarantine_pending(path)
        conn = sqlite3.connect(target, uri=uri, factory=_LockedConnection, **kwargs)
    except BaseException:
        lock.close()
        raise
    if isinstance(conn, _LockedConnection):
        conn._store_lock = lock
    else:  # a test double replaced sqlite3.connect
        lock.close()
    return conn


def connect(path: Path | str, **kwargs: Any) -> sqlite3.Connection:
    """Open a store; a writable connection would recreate a database being quarantined."""
    return _admit(Path(path), str(path), False, kwargs)


def connect_readonly(path: Path | str, **kwargs: Any) -> sqlite3.Connection:
    """Open a store read-only under the same admission rule as ``connect``."""
    path = Path(path)
    return _admit(path, path.resolve().as_uri() + "?mode=ro", True, kwargs)


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def quarantine_database(path: Path, reason: object, *, expected: tuple[int, int] | None = None) -> None:
    """Keep the database and its journals together; a failed move aborts startup.

    The private, unique directory prevents overwriting a previous recovery copy.
    Rename stays on the same filesystem and never falls back to deleting files.
    Call it while holding the exclusive store lock (``store_startup``). With
    ``expected``, refuse to move a file that is no longer the one checked.
    """
    if expected is not None and _identity(path) != expected:
        raise RuntimeError(f"Database changed during its startup check; left in place: {path}")
    destination = path.with_name(f"{path.name}.broken-{int(time.time())}-{uuid4().hex}")
    destination.mkdir(mode=0o700)
    print(f"[sqlite] Corrupt database ({reason}); preserving files in {destination}", flush=True)
    pending = path.with_name(f"{path.name}.quarantine-pending")
    with pending.open("x") as marker:
        marker.write(str(destination))
    path.rename(destination / path.name)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.rename(destination / sidecar.name)
    pending.unlink()


def check_quarantine_pending(path: Path) -> None:
    """Refuse new connections while database recovery is incomplete."""
    pending = path.with_name(f"{path.name}.quarantine-pending")
    if pending.exists():
        raise RuntimeError(f"Unfinished database quarantine: {pending}; restore or finish it before startup")


def _verify(path: Path, full_check: bool, exclusive: bool) -> None:
    """Quarantine confirmed corruption; retry other database errors, then raise.

    Three attempts, each with SQLite's five-second busy wait and a short delay,
    bound lock contention to roughly 16 seconds. Do not change journal mode
    while checking: verification should not modify a healthy database.
    """
    for attempt in range(3):
        identity = _identity(path)
        if identity is None:
            return
        problem: object = None
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
            try:
                query = "PRAGMA integrity_check" if full_check else "SELECT name FROM sqlite_master LIMIT 1"
                rows = conn.execute(query).fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError as error:
            if not is_corruption(error):
                if attempt == 2:
                    print(f"[sqlite] Store startup failed; database left in place: {path}: {error}", flush=True)
                    raise
                time.sleep(0.25)
                continue
            problem = error
        else:
            if full_check and rows != [("ok",)]:
                problem = f"integrity_check returned {rows!r}"
        if problem is None:
            return
        if not exclusive:
            raise RuntimeError(f"Corrupt database is in use; stop its other users, then restart: {path}: {problem}")
        quarantine_database(path, problem, expected=identity)
        return


@contextmanager
def store_startup(path: Path, *, full_check: bool = True) -> Iterator[None]:
    """Validate, quarantine if corrupt, and let the caller initialize under one lock.

    Startup takes the exclusive lock when no admitted connection is open. When
    one is open (a running gateway or maintenance script), it waits for a shared
    lock instead: a healthy store still initializes, a corrupt one is left in
    place. Initialize with plain ``sqlite3.connect`` or these helpers inside the
    block; this thread's nested connects reuse the held lock.
    """
    path = Path(path)
    private_io.ensure_private_dir(path.parent)
    key = lock_path(path)
    if key in _held_locks():
        check_quarantine_pending(path)
        _verify(path, full_check, exclusive=False)
        yield
        return
    with _open_lock(path) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            exclusive = True
        except BlockingIOError:
            fcntl.flock(lock, fcntl.LOCK_SH)
            exclusive = False
        _held_locks().add(key)
        try:
            check_quarantine_pending(path)
            _verify(path, full_check, exclusive)
            yield
        finally:
            _held_locks().discard(key)


def check_database(path: Path, *, full_check: bool = True) -> None:
    """Run the startup check alone (no initialization under the lock)."""
    with store_startup(path, full_check=full_check):
        pass

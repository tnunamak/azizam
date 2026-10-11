"""Check existing SQLite state without mistaking contention for corruption."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from uuid import uuid4


def is_corruption(error: sqlite3.DatabaseError) -> bool:
    code = getattr(error, "sqlite_errorcode", 0)
    return (code & 0xFF) in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} or any(
        text in str(error).lower()
        for text in ("file is not a database", "database disk image is malformed", "malformed database schema")
    )


def quarantine_database(path: Path, reason: object) -> None:
    """Keep the database and its journals together; a failed move aborts startup.

    The private, unique directory prevents overwriting a previous recovery copy.
    Rename stays on the same filesystem and never falls back to deleting files.
    Run only during store startup, after closing the checking connection.
    """
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


def check_database(path: Path) -> None:
    """Quarantine confirmed corruption; retry other database errors, then raise.

    Three attempts, each with SQLite's five-second busy wait and a short delay,
    bound lock contention to roughly 16 seconds. Do not change journal mode
    while checking: verification should not modify a healthy database.
    """
    pending = path.with_name(f"{path.name}.quarantine-pending")
    if pending.exists():
        raise RuntimeError(f"Unfinished database quarantine: {pending}; restore or finish it before startup")
    if not path.exists():
        return
    for attempt in range(3):
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
            try:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError as error:
            if is_corruption(error):
                quarantine_database(path, error)
                return
            if attempt == 2:
                print(f"[sqlite] Store startup failed; database left in place: {path}: {error}", flush=True)
                raise
            time.sleep(0.25)
        else:
            if rows != [("ok",)]:
                quarantine_database(path, f"integrity_check returned {rows!r}")
            return

#!/usr/bin/env python3
"""Read-only, quiesced-store rollback readiness snapshot.

Stop submissions before invoking this command and keep them stopped through
rollback; this snapshot is not an admission lock. A store whose WAL still holds
committed frames (a running gateway) is read through SQLite's read-only WAL path
so those frames are seen; a checkpointed store is read immutably so SQLite
creates nothing next to it. A nonempty rollback journal (an interrupted write)
is refused.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile

# Support invocation from any cwd without importing the large gateway entrypoint.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class StoreRefused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def fingerprint(path: Path) -> tuple[object, ...]:
    """File identity, times and a content hash: a same-size rewrite within one
    timestamp tick still changes the hash."""
    value = path.stat()
    if not stat.S_ISREG(value.st_mode):
        raise StoreRefused("store_unreadable")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns, digest.hexdigest())


def _companion_size(path: Path, suffix: str) -> int | None:
    companion = Path(str(path) + suffix)
    try:
        value = companion.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode):
        raise StoreRefused("store_unreadable")
    return value.st_size


def require_no_hot_journal(path: Path) -> None:
    if _companion_size(path, "-journal"):
        raise StoreRefused("store_unreadable")


def has_live_wal(path: Path) -> bool:
    """A nonempty WAL holds committed frames an immutable read would not see."""
    return bool(_companion_size(path, "-wal"))


def refused(reason: str) -> tuple[int, dict]:
    return 3, {"ready": False, "reason": reason, "job_ids": [], "inventory": []}


def inspect_store(store: str) -> tuple[int, dict]:
    try:
        source = Path(store).resolve(strict=True)
        before = fingerprint(source)
    except FileNotFoundError:
        return refused("store_missing")
    except (OSError, RuntimeError, StoreRefused):
        return refused("store_unreadable")

    try:
        require_no_hot_journal(source)
        live_wal = has_live_wal(source)
        with tempfile.TemporaryDirectory(prefix="rollback-readiness-") as directory:
            snapshot = Path(directory) / "store.sqlite"
            # mode=ro prohibits writes to the database and its WAL. With a live
            # WAL, SQLite's read-only WAL path reads the committed frames (it
            # uses the existing -shm), and the backup is one consistent read
            # even if the running gateway checkpoints meanwhile. Without one,
            # immutable keeps SQLite from creating a WAL or -shm next to the
            # store, and the unchanged fingerprint (with a content hash) shows
            # the copy is whole.
            uri = source.as_uri() + ("?mode=ro" if live_wal else "?mode=ro&immutable=1")
            with closing(sqlite3.connect(uri, uri=True)) as reader:
                with closing(sqlite3.connect(str(snapshot))) as writer:
                    reader.backup(writer)
            require_no_hot_journal(source)
            if not live_wal and (has_live_wal(source) or fingerprint(source) != before):
                raise StoreRefused("store_unreadable")
            # A store with no job table (a 0-byte file, a wrong database)
            # proves nothing about running jobs; the helpers would call it empty.
            with closing(sqlite3.connect(str(snapshot))) as copy:
                has_jobs = copy.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_jobs'"
                ).fetchone()
            if not has_jobs:
                raise StoreRefused("store_unreadable")

            # Both helpers receive only the disposable copy. Their connection
            # pragmas may create WAL files, all inside the temporary directory.
            from gateway.workflow_run import (
                RollbackRefused,
                assert_rollback_ready,
                rollback_job_inventory,
            )

            inventory = rollback_job_inventory(snapshot)
            try:
                assert_rollback_ready(snapshot)
            except RollbackRefused as error:
                return 2, {
                    "ready": False, "reason": error.code,
                    "job_ids": error.job_ids, "inventory": inventory,
                }
            return 0, {
                "ready": True, "reason": None, "job_ids": [], "inventory": inventory,
            }
    except StoreRefused as error:
        return refused(error.reason)
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, ImportError):
        # Includes unreadable SQLite headers/schema/rows (a job_json that is
        # valid JSON but not an object), failed snapshots and a gateway
        # module that cannot import.
        return refused("store_unreadable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, help="the job store, with submissions stopped")
    parser.add_argument("--json", action="store_true", help="emit one JSON result object")
    args = parser.parse_args(argv)
    code, result = inspect_store(args.store)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for row in result["inventory"]:
            print(json.dumps(row, sort_keys=True))
        if result["job_ids"]:
            print("JOB_IDS " + json.dumps(result["job_ids"]))
        print("READY" if result["ready"] else "REFUSED " + result["reason"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())

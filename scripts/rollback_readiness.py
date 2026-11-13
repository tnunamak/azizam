#!/usr/bin/env python3
"""Read-only, quiesced-store rollback readiness snapshot.

Stop submissions before invoking this command and keep them stopped through
rollback; this snapshot is not an admission lock. A store whose WAL still holds
committed frames is read through SQLite's read-only WAL path when SHM exists.
Without SHM, the database and WAL are copied privately before SQLite opens them.
A checkpointed store is read immutably so SQLite creates nothing next to it.
A nonempty rollback journal (an interrupted write) is refused.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

# Support invocation from any cwd without importing the large gateway entrypoint.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# One budget covers private copying, verification and SQLite backup retries.
BACKUP_TIMEOUT_SECONDS = 5.0

# The commit that taught the job store's readers tombstones (JobService.delete: a deleted job is hidden from every
# workflow read and listing, and reaped after its window). A rollback target that contains it hides a deleted job from
# the same reads as this build, so deleted rows the reaper has not removed yet cannot resurface after the switch.
# If a later change makes more reads hide deleted jobs, move this to that commit: an older target would show them again.
# Store reads also filter from T60 onward; the threshold stays because higher-level reads already honour tombstones.
TOMBSTONE_READER_COMMIT = "fc6e2b36d7fea6afbb23a8cb1679f0dbe5e29434"
REPO = Path(__file__).resolve().parents[1]


def target_reads_tombstones(rev: str | None, repo: Path = REPO) -> bool:
    """True when ``rev`` names a commit that contains TOMBSTONE_READER_COMMIT. Anything git cannot answer is False."""
    if not rev or rev.startswith("-"):
        return False
    try:
        result = subprocess.run(
            # The real history decides: a local replace ref must not make an older target look newer.
            ["git", "--no-replace-objects", "-C", str(repo), "merge-base", "--is-ancestor", TOMBSTONE_READER_COMMIT, rev],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class StoreRefused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _check_snapshot_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise StoreRefused("store_unreadable")


def _copy_file(source: Path, destination: Path, deadline: float) -> None:
    """Copy bounded chunks without giving the private-copy path a fresh budget."""
    _check_snapshot_deadline(deadline)
    with open(source, "rb") as reader, open(destination, "wb") as writer:
        while True:
            _check_snapshot_deadline(deadline)
            block = reader.read(1 << 20)
            _check_snapshot_deadline(deadline)
            if not block:
                return
            writer.write(block)


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


def inspect_store(store: str, *, reads_tombstones: bool = False) -> tuple[int, dict]:
    deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS
    try:
        source = Path(store).resolve(strict=True)
        before = fingerprint(source)
    except FileNotFoundError:
        return refused("store_missing")
    except (OSError, RuntimeError, StoreRefused):
        return refused("store_unreadable")

    try:
        _check_snapshot_deadline(deadline)
        require_no_hot_journal(source)
        live_wal = has_live_wal(source)
        with tempfile.TemporaryDirectory(prefix="rollback-readiness-") as directory:
            snapshot = Path(directory) / "store.sqlite"
            read_source = source
            wal = Path(str(source) + "-wal")
            copied_wal_before = None
            if live_wal and _companion_size(source, "-shm") is None:
                # mode=ro can create SHM when it is absent. Reconstruct it only
                # beside this private copy, never beside the original store.
                copied_wal_before = fingerprint(wal)
                read_source = Path(directory) / "source.sqlite"
                _copy_file(source, read_source, deadline)
                _copy_file(wal, Path(str(read_source) + "-wal"), deadline)
                require_no_hot_journal(source)
                if fingerprint(source) != before or fingerprint(wal) != copied_wal_before:
                    raise StoreRefused("store_unreadable")
            _check_snapshot_deadline(deadline)
            # Existing WAL+SHM keeps its live, consistent SQLite backup path.
            # A WAL copy must not be immutable: its committed frames matter.
            uri = read_source.as_uri() + ("?mode=ro" if live_wal else "?mode=ro&immutable=1")

            def check_deadline(_status: int, _remaining: int, _total: int) -> None:
                # SQLite also calls progress for BUSY/LOCKED retries.
                if time.monotonic() >= deadline:
                    raise StoreRefused("store_unreadable")

            with closing(sqlite3.connect(uri, uri=True, timeout=0)) as reader:
                with closing(sqlite3.connect(str(snapshot))) as writer:
                    reader.backup(writer, pages=128, progress=check_deadline, sleep=0.01)
            require_no_hot_journal(source)
            if copied_wal_before is not None:
                if fingerprint(source) != before or fingerprint(wal) != copied_wal_before:
                    raise StoreRefused("store_unreadable")
            elif not live_wal and (has_live_wal(source) or fingerprint(source) != before):
                raise StoreRefused("store_unreadable")
            _check_snapshot_deadline(deadline)
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

            _check_snapshot_deadline(deadline)
            inventory = rollback_job_inventory(snapshot)
            _check_snapshot_deadline(deadline)
            try:
                assert_rollback_ready(snapshot, target_reads_tombstones=reads_tombstones)
            except RollbackRefused as error:
                verdict = 2, {
                    "ready": False, "reason": error.code,
                    "job_ids": error.job_ids, "inventory": inventory,
                }
            else:
                verdict = 0, {
                    "ready": True, "reason": None, "job_ids": [], "inventory": inventory,
                }
            _check_snapshot_deadline(deadline)
        # Cleanup is part of the same budget; do not return a verdict from
        # inside the context while private files are still being removed.
        _check_snapshot_deadline(deadline)
        return verdict
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
    parser.add_argument("--target-commit", help="the build the rollback switches to; a target that hides deleted jobs "
                        "is not held by deleted rows the reaper has not removed yet")
    args = parser.parse_args(argv)
    reads = target_reads_tombstones(args.target_commit)
    code, result = inspect_store(args.store, reads_tombstones=reads)
    if args.target_commit is not None:
        result = {**result, "target_reads_tombstones": reads}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for row in result["inventory"]:
            print(json.dumps(row, sort_keys=True))
        if result["job_ids"]:
            print("JOB_IDS " + json.dumps(result["job_ids"]))
        if args.target_commit is not None:
            print("TARGET_READS_TOMBSTONES " + ("yes" if reads else "no"))
        print("READY" if result["ready"] else "REFUSED " + result["reason"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())

"""Snapshot lifecycle for a residency domain (design D5, slot T1.3).

A snapshot is private session cache that a checkpoint wrote to disk. This module keeps the ledger
of those files in ``data/transition.db`` (its own tables) and the private directory they live in.

Lifecycle: ``pending`` -> ``published`` -> ``abandoned`` -> ``deleted``. Only ``pending`` can become
``published``; nothing leaves ``abandoned`` or ``deleted``.

- The generation number is the row id of an ``AUTOINCREMENT`` table. A row is never removed, so a
  generation is never reused.
- Publishing is one transaction that moves the per-domain pointer. Before it, the file is
  verified: it exists, is a private regular file, still has the size the checkpoint reported and
  the identity (inode, modification time) recorded at ``pending``, and fits the budget. A crash
  before the transaction leaves the old snapshot published; a crash after it leaves the new one.
  Never a half-written one, because the pointer moves only after verification.
- A snapshot with no byte count or no compatibility token is never published (D5 "completion and
  integrity evidence").
- Files are private: the directory is owned by this process and closed to others; a file must be
  a regular, single-link file owned by this process. The domain's disk use is bounded by a
  declared ``SnapshotBudget``; a new checkpoint is refused when the domain is full.
- Deletion goes only through ``collect``. It removes a file only when a ledger row owns it, the
  row is ``abandoned`` and not pinned, and the file on disk is still the one the row recorded
  (same file id, size and modification time). A file with no row is an orphan: it counts against
  the budget and is reported, never deleted.
- A handle is unique per checkpoint effect, per domain: ``pending`` refuses a handle that any earlier
  row of the domain used, ``deleted`` rows included. An adapter that reuses a handle breaks the contract;
  the refusal names it. The same handle in two domains is two different files and is allowed.

The store compares compatibility maps and nothing else about an engine (D5 "Compatibility"). It
names no engine, model or vendor.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Mapping, Optional, Sequence

from gateway.transition_ops import Snapshot, SnapshotState

SNAPSHOT_STATES: tuple[str, ...] = ("pending", "published", "abandoned", "deleted")

_HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transition_snapshot (
    generation    INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT NOT NULL,
    handle        TEXT NOT NULL,
    op_id         TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('pending', 'published', 'abandoned', 'deleted')),
    compatibility TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    file_size     INTEGER NOT NULL,
    file_mtime_ns INTEGER NOT NULL,
    file_id       TEXT NOT NULL,
    pinned        INTEGER NOT NULL DEFAULT 0,
    created       REAL NOT NULL,
    updated       REAL NOT NULL
);
DROP INDEX IF EXISTS transition_snapshot_live_handle;
CREATE UNIQUE INDEX IF NOT EXISTS transition_snapshot_live_domain_handle
    ON transition_snapshot(domain, handle) WHERE state != 'deleted';
CREATE INDEX IF NOT EXISTS transition_snapshot_domain ON transition_snapshot(domain, state);
CREATE TABLE IF NOT EXISTS transition_snapshot_pointer (
    domain     TEXT PRIMARY KEY,
    generation INTEGER NOT NULL REFERENCES transition_snapshot(generation)
);
"""


class SnapshotRejected(Exception):
    """The snapshot cannot be recorded, published or deleted. The reason is for the receipt."""


@dataclass(frozen=True)
class SnapshotBudget:
    """The declared bound for one domain's snapshot files."""

    max_bytes: int
    max_count: int

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.max_count <= 0:
            raise ValueError("a snapshot budget needs a positive byte limit and a positive count limit")


@dataclass(frozen=True)
class FileFacts:
    size: int
    mtime_ns: int
    file_id: str
    """``device:inode``: the identity of the file, not of the path."""


class PrivateSnapshotDir:
    """The private directory snapshot files live in, one subdirectory per domain.

    The adapter writes a file at ``path_for(domain, handle)``; core checks the handle against a
    fixed pattern and never takes a path from an adapter reply.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._require_private_dir(self.root)

    @staticmethod
    def _require_private_dir(path: Path) -> None:
        facts = path.lstat()
        if not stat.S_ISDIR(facts.st_mode) or facts.st_uid != os.getuid() or facts.st_mode & 0o077:
            raise SnapshotRejected(f"snapshot directory {path.name} is not a private directory of this process")

    def _domain_dir(self, domain: str) -> Path:
        path = self.root / hashlib.sha256(domain.encode()).hexdigest()[:32]
        path.mkdir(exist_ok=True, mode=0o700)
        self._require_private_dir(path)
        return path

    def path_for(self, domain: str, handle: str) -> Path:
        if not _HANDLE.fullmatch(handle):
            raise SnapshotRejected("handle does not match the snapshot handle pattern")
        return self._domain_dir(domain) / handle

    def facts(self, domain: str, handle: str) -> Optional[FileFacts]:
        """The file's facts, ``None`` when it does not exist. A symlink, a hard-linked file, a
        file of another owner or a non-regular file is rejected, never described."""
        path = self.path_for(domain, handle)
        try:
            facts = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(facts.st_mode) or facts.st_nlink != 1 or facts.st_uid != os.getuid():
            raise SnapshotRejected("snapshot file is not a private regular file of this process")
        return FileFacts(size=facts.st_size, mtime_ns=facts.st_mtime_ns, file_id=f"{facts.st_dev}:{facts.st_ino}")

    def remove(self, domain: str, handle: str, expected: FileFacts) -> bool:
        """Remove the file if it is still the recorded one (file id, size and modification time).
        ``False``: already gone."""
        facts = self.facts(domain, handle)
        if facts is None:
            return False
        if facts != expected:
            raise SnapshotRejected("the file at this handle is not the one the ledger recorded")
        self.path_for(domain, handle).unlink()
        return True

    def names(self, domain: str) -> list[str]:
        return sorted(entry.name for entry in self._domain_dir(domain).iterdir())

    def usage(self, domain: str) -> tuple[int, int]:
        """``(file count, bytes)`` on disk for the domain, orphans included."""
        count = total = 0
        for entry in self._domain_dir(domain).iterdir():
            facts = entry.lstat()
            count += 1
            total += facts.st_size if stat.S_ISREG(facts.st_mode) else 0
        return count, total


def compatible(saved: Mapping[str, str], current: Mapping[str, str], ignore: Collection[str] = ()) -> bool:
    """True when both maps are non-empty and agree on every key except the ``ignore`` ones.

    ``ignore`` is the reviewed list of placement keys that may differ. An empty map is an unknown
    token, and unknown means cold (D5). The same keys must be present on both sides.
    """
    left = {key: value for key, value in saved.items() if key not in ignore}
    right = {key: value for key, value in current.items() if key not in ignore}
    return bool(saved) and bool(current) and bool(left) and left == right


CrashHook = Callable[[str], None]

DEFAULT_SNAPSHOT_BUDGET = SnapshotBudget(max_bytes=20 * 1024**3, max_count=2)
"""The bound the gateway applies to each domain's snapshot files until an owner setting declares another (D5 "Retention")."""


class SqliteSnapshotStore:
    """The ledger. Every method is blocking and uses its own connection; call it off the event loop."""

    def __init__(self, path: Path | str, files: PrivateSnapshotDir, budget: SnapshotBudget, *,
                 clock: Callable[[], float] = time.time, crash_hook: Optional[CrashHook] = None) -> None:
        self.path = Path(path)
        self.files = files
        self.budget = budget
        self._clock = clock
        self._crash_hook = crash_hook
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
        finally:
            connection.close()

    # -- plumbing ---------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _hook(self, point: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(point)

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> Snapshot:
        return Snapshot(handle=row["handle"], domain=row["domain"], op_id=row["op_id"], state=row["state"],
                        compatibility=json.loads(row["compatibility"]), bytes=row["bytes"], created=row["created"])

    @staticmethod
    def _live_row(connection: sqlite3.Connection, handle: str, domain: Optional[str] = None) -> Optional[sqlite3.Row]:
        """The row for a live handle. Handles are unique per domain, so a call that omits the domain
        must find at most one row; the same handle live in two domains is ambiguous and is refused."""
        query, args = "SELECT * FROM transition_snapshot WHERE handle = ? AND state != 'deleted'", (handle,)
        if domain is not None:
            query, args = query + " AND domain = ?", (handle, domain)
        rows = connection.execute(query, args).fetchall()
        if len(rows) > 1:
            raise SnapshotRejected("the handle is live in more than one domain; name the domain")
        return rows[0] if rows else None

    # -- lifecycle --------------------------------------------------------------

    def pending(self, *, domain: str, op_id: str, handle: str, compatibility: Mapping[str, str],
                bytes: Optional[int]) -> Snapshot:
        """Record the file a checkpoint wrote. It is not servable until ``publish``."""
        if not compatibility:
            raise SnapshotRejected("no compatibility token")
        if bytes is None or bytes < 0:
            raise SnapshotRejected("no byte count from the checkpoint")
        facts = self.files.facts(domain, handle)
        if facts is None:
            raise SnapshotRejected("the checkpoint reported success but no file exists")
        now = self._clock()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    earlier = connection.execute(
                        "SELECT state FROM transition_snapshot WHERE domain = ? AND handle = ? "
                        "ORDER BY generation DESC LIMIT 1", (domain, handle)).fetchone()
                    if earlier is not None:
                        # A handle names one file once. An adapter that answers every checkpoint with the same
                        # handle would overwrite the file of a snapshot the ledger still trusts (or already deleted).
                        raise SnapshotRejected(
                            "handle already belongs to a live snapshot of this domain; the adapter contract is one "
                            "unique handle per checkpoint effect" if earlier["state"] != "deleted" else
                            "handle was used by an earlier snapshot of this domain (deleted rows count): "
                            "every checkpoint needs a new handle")
                    connection.execute(
                        "INSERT INTO transition_snapshot(domain, handle, op_id, state, compatibility, bytes, file_size,"
                        " file_mtime_ns, file_id, created, updated) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                        (domain, handle, op_id, json.dumps(dict(compatibility), sort_keys=True), bytes, facts.size,
                         facts.mtime_ns, facts.file_id, now, now))
                    row = self._live_row(connection, handle, domain)
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
        assert row is not None
        return self._snapshot(row)

    def publish(self, handle: str, *, domain: Optional[str] = None) -> Snapshot:
        """Verify the pending snapshot, then move the domain's pointer to it in one transaction."""
        with self._lock:
            connection = self._connect()
            try:
                row = self._live_row(connection, handle, domain)
                if row is None:
                    raise SnapshotRejected("unknown snapshot")
                if row["state"] != "pending":
                    raise SnapshotRejected(f"a {row['state']} snapshot cannot be published")
                self._verify_pending(row)
                self._hook("publish:verified")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current = connection.execute("SELECT state FROM transition_snapshot WHERE generation = ?",
                                                 (row["generation"],)).fetchone()
                    if current is None or current["state"] != "pending":
                        raise SnapshotRejected("the snapshot is no longer pending")
                    now = self._clock()
                    pointer = connection.execute("SELECT generation FROM transition_snapshot_pointer WHERE domain = ?",
                                                 (row["domain"],)).fetchone()
                    if pointer is not None:
                        if pointer["generation"] > row["generation"]:
                            raise SnapshotRejected("an older snapshot cannot replace a newer published one")
                        connection.execute(
                            "UPDATE transition_snapshot SET state = 'abandoned', updated = ? WHERE generation = ?"
                            " AND state = 'published'", (now, pointer["generation"]))
                    connection.execute("UPDATE transition_snapshot SET state = 'published', updated = ?"
                                       " WHERE generation = ?", (now, row["generation"]))
                    connection.execute("INSERT OR REPLACE INTO transition_snapshot_pointer(domain, generation)"
                                       " VALUES (?, ?)", (row["domain"], row["generation"]))
                    self._hook("publish:in_transaction")
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                published = self._live_row(connection, handle, row["domain"])
            finally:
                connection.close()
        assert published is not None
        self._hook("publish:committed")
        return self._snapshot(published)

    def _verify_pending(self, row: sqlite3.Row) -> None:
        facts = self.files.facts(row["domain"], row["handle"])
        if facts is None:
            raise SnapshotRejected("the snapshot file is missing")
        if facts.file_id != row["file_id"]:
            raise SnapshotRejected("the file was replaced after it was recorded")
        if facts.size != row["bytes"]:
            raise SnapshotRejected("the file size differs from the byte count the checkpoint reported (half-written)")
        if (facts.size, facts.mtime_ns) != (row["file_size"], row["file_mtime_ns"]):
            raise SnapshotRejected("the file changed after it was recorded (still being written)")
        count, total = self.files.usage(row["domain"])
        if count > self.budget.max_count or total > self.budget.max_bytes:
            raise SnapshotRejected("the domain's snapshot files exceed the declared budget")

    def abandon_pending(self, domain: str, op_id: Optional[str] = None) -> Sequence[Snapshot]:
        """Mark every pending snapshot of the domain (or of one operation) abandoned."""
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    query = "SELECT * FROM transition_snapshot WHERE domain = ? AND state = 'pending'"
                    args: tuple = (domain,)
                    if op_id is not None:
                        query += " AND op_id = ?"
                        args = (domain, op_id)
                    rows = connection.execute(query, args).fetchall()
                    for row in rows:
                        connection.execute("UPDATE transition_snapshot SET state = 'abandoned', updated = ?"
                                           " WHERE generation = ? AND state = 'pending'", (self._clock(), row["generation"]))
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
        return tuple(self._with_state(row, "abandoned") for row in rows)

    def _with_state(self, row: sqlite3.Row, state: SnapshotState) -> Snapshot:
        snapshot = self._snapshot(row)
        return Snapshot(handle=snapshot.handle, domain=snapshot.domain, op_id=snapshot.op_id, state=state,
                        compatibility=snapshot.compatibility, bytes=snapshot.bytes, created=snapshot.created)

    def retire_published(self, domain: str) -> Optional[Snapshot]:
        """Abandon the published snapshot and clear the pointer, so the next start is cold."""
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = self._pointer_row(connection, domain)
                    if row is not None:
                        connection.execute("UPDATE transition_snapshot SET state = 'abandoned', updated = ?"
                                           " WHERE generation = ? AND state = 'published'",
                                           (self._clock(), row["generation"]))
                    connection.execute("DELETE FROM transition_snapshot_pointer WHERE domain = ?", (domain,))
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
        return None if row is None else self._with_state(row, "abandoned")

    @staticmethod
    def _pointer_row(connection: sqlite3.Connection, domain: str) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT s.* FROM transition_snapshot_pointer p JOIN transition_snapshot s ON s.generation = p.generation"
            " WHERE p.domain = ? AND s.state = 'published'", (domain,)).fetchone()

    def latest_published(self, domain: str, compatibility: Mapping[str, str],
                         ignore: Collection[str] = ()) -> Optional[Snapshot]:
        """The published snapshot, if it is compatible and its file is still the one that was verified.

        A published snapshot whose file is missing or changed is abandoned, so it is never offered
        again. A compatibility mismatch leaves it published: it may be valid for another config.
        """
        with self._lock:
            connection = self._connect()
            try:
                row = self._pointer_row(connection, domain)
            finally:
                connection.close()
        if row is None or not compatible(json.loads(row["compatibility"]), compatibility, ignore):
            return None
        try:
            facts = self.files.facts(domain, row["handle"])
        except SnapshotRejected:
            facts = None
        if facts is None or (facts.file_id, facts.size, facts.mtime_ns) != (
                row["file_id"], row["bytes"], row["file_mtime_ns"]):
            self.retire_published(domain)
            return None
        return self._snapshot(row)

    # -- pins, room, deletion ---------------------------------------------------

    def pin(self, handle: str, *, domain: Optional[str] = None) -> bool:
        return self._set_pin(handle, 1, domain)

    def unpin(self, handle: str, *, domain: Optional[str] = None) -> bool:
        return self._set_pin(handle, 0, domain)

    def _set_pin(self, handle: str, value: int, domain: Optional[str]) -> bool:
        with self._lock:
            connection = self._connect()
            try:
                row = self._live_row(connection, handle, domain)
                if row is None:
                    return False
                cursor = connection.execute("UPDATE transition_snapshot SET pinned = ? WHERE generation = ?",
                                            (value, row["generation"]))
                return cursor.rowcount == 1
            finally:
                connection.close()

    def has_room(self, domain: str, expect_bytes: int = 0) -> bool:
        """True when the domain may take one more snapshot of about ``expect_bytes`` (orphans count)."""
        count, total = self.files.usage(domain)
        return count < self.budget.max_count and total < self.budget.max_bytes and (
            total + expect_bytes <= self.budget.max_bytes)

    def collect(self, domain: str) -> Sequence[str]:
        """Delete the abandoned, unpinned snapshots of the domain whose files the ledger owns.

        A file that is no longer the recorded one (another file id, size or modification time) is left
        in place and the row stays ``abandoned``: ownership is not proven, so the file is not ours to
        delete.
        """
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute("SELECT * FROM transition_snapshot WHERE domain = ? AND state = 'abandoned'"
                                          " AND pinned = 0 ORDER BY generation", (domain,)).fetchall()
                deleted: list[str] = []
                for row in rows:
                    try:
                        self.files.remove(domain, row["handle"], FileFacts(
                            size=row["file_size"], mtime_ns=row["file_mtime_ns"], file_id=row["file_id"]))
                    except SnapshotRejected as exc:
                        print(f"[transition-snapshot] not deleting {row['handle']}: {exc}", flush=True)
                        continue
                    connection.execute("UPDATE transition_snapshot SET state = 'deleted', updated = ?"
                                       " WHERE generation = ? AND state = 'abandoned' AND pinned = 0",
                                       (self._clock(), row["generation"]))
                    deleted.append(row["handle"])
                return tuple(deleted)
            finally:
                connection.close()

    def orphans(self, domain: str) -> Sequence[str]:
        """Files in the domain's directory that no live ledger row owns. Reported, never deleted."""
        with self._lock:
            connection = self._connect()
            try:
                owned = {row["handle"] for row in connection.execute(
                    "SELECT handle FROM transition_snapshot WHERE domain = ? AND state != 'deleted'", (domain,))}
            finally:
                connection.close()
        return tuple(name for name in self.files.names(domain) if name not in owned)

    def startup_reconcile(self) -> None:
        """After a restart: a pin from a dead process means nothing, and a snapshot still ``pending``
        is deleted (the profile of this build has no query that could publish it later)."""
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("UPDATE transition_snapshot SET pinned = 0 WHERE pinned != 0")
                domains = [row["domain"] for row in connection.execute(
                    "SELECT DISTINCT domain FROM transition_snapshot WHERE state IN ('pending', 'abandoned')")]
            finally:
                connection.close()
        for domain in domains:
            self.abandon_pending(domain)
            self.collect(domain)

    # -- introspection (diagnostics and tests) ---------------------------------

    def get(self, handle: str, domain: Optional[str] = None) -> Optional[Snapshot]:
        with self._lock:
            connection = self._connect()
            try:
                row = self._live_row(connection, handle, domain)
            finally:
                connection.close()
        return None if row is None else self._snapshot(row)

    def generation_of(self, handle: str, domain: Optional[str] = None) -> Optional[int]:
        with self._lock:
            connection = self._connect()
            try:
                row = self._live_row(connection, handle, domain)
            finally:
                connection.close()
        return None if row is None else int(row["generation"])

    def rows(self, domain: str) -> Sequence[tuple[int, str, str]]:
        """``(generation, handle, state)`` for every row of the domain, deleted ones included."""
        with self._lock:
            connection = self._connect()
            try:
                return tuple((r["generation"], r["handle"], r["state"]) for r in connection.execute(
                    "SELECT generation, handle, state FROM transition_snapshot WHERE domain = ? ORDER BY generation",
                    (domain,)))
            finally:
                connection.close()

    def pointer(self, domain: str) -> Optional[str]:
        with self._lock:
            connection = self._connect()
            try:
                row = self._pointer_row(connection, domain)
            finally:
                connection.close()
        return None if row is None else row["handle"]


class LazySnapshotStore:
    """A snapshot store that opens its directory and ledger on first use.

    The gateway builds its transition runtime when ``proxy`` is imported, and an import reads no path. The runtime keeps
    this object; the first call, normally the startup hook ``startup_reconcile`` the lifespan runs before any
    admission, calls ``open_store`` once. Every other method is the real store's, by name.
    """

    def __init__(self, open_store: Callable[[], SqliteSnapshotStore]) -> None:
        self._open_store = open_store
        self._store: Optional[SqliteSnapshotStore] = None
        self._lock = threading.Lock()

    def opened(self) -> SqliteSnapshotStore:
        with self._lock:
            if self._store is None:
                self._store = self._open_store()
            return self._store

    def startup_reconcile(self) -> None:
        self.opened().startup_reconcile()

    def __getattr__(self, name: str):  # only names not defined above reach here
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.opened(), name)

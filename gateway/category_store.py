"""Durable opaque policy snapshots; not wired into request handling.

Callers must validate/preview candidates before activation and deny protected
access on NoActiveSnapshotError, CorruptSnapshotError or sqlite3 errors.
The durable active_revision_counter changes in the same commit as activation;
cache users must check it before reuse. No process-local cache is managed here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


class StaleBaseError(RuntimeError):
    """The expected active revision or the candidate's base is stale."""


class NoActiveSnapshotError(RuntimeError):
    """No committed active policy exists; protected access must deny."""


class CorruptSnapshotError(RuntimeError):
    """Snapshot integrity or durable active state could not be verified."""


class SnapshotNotFoundError(LookupError):
    """The requested immutable revision does not exist."""


def _canonical(body: Any) -> str:
    # Reject Python-only values and key coercion rather than silently changing
    # the caller's document. This is deterministic JSON, not RFC 8785/JCS.
    def validate(value: object) -> None:
        if value is None or type(value) in (str, bool, int, float):
            return
        if type(value) is list:
            for item in value:
                validate(item)
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for item in value.values():
                validate(item)
            return
        raise TypeError("snapshot body must contain only JSON values")
    validate(body)
    result = json.dumps(body, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False)
    result.encode("utf-8")  # reject unpaired surrogates before writing
    return result


def _hash(body_json: str) -> str:
    return hashlib.sha256(body_json.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _revision(value: object, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not int or value < 1:
        raise ValueError("revision must be a positive integer or an allowed None")


@dataclass(frozen=True)
class Snapshot:
    revision: int
    parent_revision: int | None
    body_json: str
    body_sha256: str
    created_at: str
    author: str
    reason: str
    active_counter: int | None = None

    @property
    def body(self) -> Any:
        """Return a fresh decoded document; mutations cannot change this row."""
        return json.loads(self.body_json)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_snapshots (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_revision INTEGER REFERENCES policy_snapshots(revision),
    body_json TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_active (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER REFERENCES policy_snapshots(revision),
    counter INTEGER NOT NULL CHECK (counter >= 0)
);
INSERT OR IGNORE INTO policy_active VALUES (1, NULL, 0);
CREATE TABLE IF NOT EXISTS policy_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL CHECK (action IN ('activate', 'restore')),
    revision INTEGER NOT NULL REFERENCES policy_snapshots(revision),
    previous_revision INTEGER REFERENCES policy_snapshots(revision),
    source_revision INTEGER REFERENCES policy_snapshots(revision),
    active_counter INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS policy_snapshots_no_update
BEFORE UPDATE ON policy_snapshots BEGIN
    SELECT RAISE(ABORT, 'immutable snapshot');
END;
CREATE TRIGGER IF NOT EXISTS policy_snapshots_no_delete
BEFORE DELETE ON policy_snapshots BEGIN
    SELECT RAISE(ABORT, 'immutable snapshot');
END;
CREATE TRIGGER IF NOT EXISTS policy_audit_no_update
BEFORE UPDATE ON policy_audit BEGIN
    SELECT RAISE(ABORT, 'append-only audit');
END;
CREATE TRIGGER IF NOT EXISTS policy_audit_no_delete
BEFORE DELETE ON policy_audit BEGIN
    SELECT RAISE(ABORT, 'append-only audit');
END;
"""


class CategoryStore:
    """File-backed store using short-lived connections; safe across processes.

    None denotes the initial empty base. Revisions identify immutable candidates;
    the separate activation counter advances only after successful activation.
    history() returns the latest audit events in ascending sequence order.
    sqlite3 operational errors propagate and never reset or delete policy data.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) == ":memory:":
            raise ValueError("a persistent database path is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            result = conn.execute("PRAGMA integrity_check").fetchall()
            if len(result) != 1 or result[0][0] != "ok":
                raise CorruptSnapshotError("SQLite integrity check failed")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise sqlite3.DatabaseError("unsupported policy store schema version")
            # executescript owns this transaction, including concurrent startup.
            if version == 0:
                conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA +
                                   f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
        try:
            self.get_active()
        except NoActiveSnapshotError:
            pass  # Empty initialization is allowed, but reads explicitly deny.

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA temp_store=MEMORY")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _get(self, conn: sqlite3.Connection, revision: int | None,
             counter: int | None = None) -> Snapshot:
        row = conn.execute(
            "SELECT * FROM policy_snapshots WHERE revision=?", (revision,)
        ).fetchone()
        if row is None:
            raise SnapshotNotFoundError(revision)
        try:
            text = row["body_json"]
            if _hash(text) != row["body_sha256"]:
                raise ValueError("hash mismatch")
            if _canonical(json.loads(text)) != text:
                raise ValueError("noncanonical JSON")
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise CorruptSnapshotError(f"invalid snapshot {revision}") from exc
        return Snapshot(**dict(row), active_counter=counter)

    def _state(self, conn):
        row = conn.execute(
            "SELECT revision, counter FROM policy_active WHERE singleton=1"
        ).fetchone()
        if row is None or (row["revision"] is None and row["counter"] != 0):
            raise CorruptSnapshotError("invalid active pointer")
        if row["revision"] is not None:
            if row["counter"] <= 0:
                raise CorruptSnapshotError("invalid activation counter")
            try:
                self._get(conn, row["revision"])
            except SnapshotNotFoundError as exc:
                raise CorruptSnapshotError("active snapshot is missing") from exc
        return row["revision"], row["counter"]

    def _check_base(self, conn, expected):
        active, counter = self._state(conn)
        if active != expected:
            raise StaleBaseError(f"expected active {expected}, found {active}")
        return active, counter

    def _insert(self, conn, body_json, base_revision, author, reason):
        if not isinstance(author, str) or not author.strip():
            raise ValueError("author must be nonempty")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be nonempty")
        cursor = conn.execute(
            "INSERT INTO policy_snapshots "
            "(parent_revision, body_json, body_sha256, created_at, author, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (base_revision, body_json, _hash(body_json), _now(), author, reason),
        )
        return self._get(conn, cursor.lastrowid)

    def _append_audit(self, conn, action, snapshot, previous, source, counter):
        conn.execute(
            "INSERT INTO policy_audit "
            "(action, revision, previous_revision, source_revision, "
            "active_counter, created_at, author, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (action, snapshot.revision, previous, source, counter, _now(),
             snapshot.author, snapshot.reason),
        )

    def create_candidate(self, body, base_revision, author, reason) -> Snapshot:
        """Persist a candidate against the current base without activating it."""
        _revision(base_revision, optional=True)
        body_json = _canonical(body)
        with self._transaction() as conn:
            self._check_base(conn, base_revision)
            return self._insert(conn, body_json, base_revision, author, reason)

    def activate(self, candidate_revision, expected_active_revision) -> Snapshot:
        """Atomically commit the pointer, counter and audit, or change nothing."""
        _revision(candidate_revision)
        _revision(expected_active_revision, optional=True)
        with self._transaction() as conn:
            previous, counter = self._check_base(conn, expected_active_revision)
            snapshot = self._get(conn, candidate_revision)
            if snapshot.parent_revision != expected_active_revision:
                raise StaleBaseError("candidate was previewed against another base")
            conn.execute(
                "UPDATE policy_active SET revision=?, counter=? WHERE singleton=1",
                (candidate_revision, counter + 1),
            )
            self._append_audit(conn, "activate", snapshot, previous, None, counter + 1)
            return self._get(conn, candidate_revision, counter + 1)

    def restore(self, old_revision, base_revision, author, reason=None) -> Snapshot:
        """Copy verified old data to a NEW candidate; callers revalidate/preview.

        ``reason`` is recorded with the candidate and its restore audit row; it
        defaults to naming the restored revision.
        """
        _revision(old_revision)
        _revision(base_revision, optional=True)
        with self._transaction() as conn:
            previous, counter = self._check_base(conn, base_revision)
            old = self._get(conn, old_revision)
            text = (reason or "").strip() or f"restore revision {old_revision}"
            snapshot = self._insert(conn, old.body_json, base_revision, author, text)
            self._append_audit(conn, "restore", snapshot, previous, old_revision, counter)
            return snapshot

    def get(self, revision) -> Snapshot:
        _revision(revision)
        with self._connection() as conn:
            return self._get(conn, revision)

    def get_active(self) -> Snapshot:
        # A read transaction keeps pointer and body/counter from different
        # commits from being combined during concurrent activation.
        with self._connection() as conn:
            conn.execute("BEGIN")
            revision, counter = self._state(conn)
            if revision is None:
                raise NoActiveSnapshotError("no valid committed policy snapshot")
            return self._get(conn, revision, counter)

    @property
    def active_revision_counter(self) -> int:
        with self._connection() as conn:
            conn.execute("BEGIN")
            return self._state(conn)[1]

    def revisions(self) -> list[int]:
        """Every stored snapshot revision, oldest first (candidates included)."""
        with self._connection() as conn:
            return [row[0] for row in conn.execute(
                "SELECT revision FROM policy_snapshots ORDER BY revision")]

    def history(self, limit=100) -> list[dict]:
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a nonnegative integer")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM (SELECT * FROM policy_audit "
                "ORDER BY sequence DESC LIMIT ?) ORDER BY sequence", (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

"""Durable shadow ledger: an SQLite adapter for operations, fencing generations and shadow comparisons.

Design §4.1, §4.3, §14 and §16 slice 1. This module is an adapter. It deals in ResourceRef, integer
quantities, units and operation keys, and it names no engine, device or domain.

Rules the store keeps:
- Every write is one BEGIN IMMEDIATE transaction, COMMIT included. The database is in WAL mode.
  A failed write rolls back, so a failed commit cannot leave a transaction open.
- Every method holds one lock, so one store object is safe to share between threads.
- acquire checks the resource generation and records the operation in the same transaction (A1).
  A unit that differs from an unsettled operation on the same incarnation is refused (§16 criterion 5).
- settle releases only on STOCK_RELEASED evidence for the same resource and operation (§4.4, B1).
  EXECUTION_ENDED records that the execution ended; it never releases. A repeated settle changes nothing.
- take_over raises the generation and moves the older generation's open operations to reconciling.
  They stay charged until settled (A4, §4.3).
- charge counts every operation that is not settled, whatever its state (§4.2).
- read_snapshot opens an existing ledger read-only, validates it, and never creates, migrates or writes.

The store records the shadow. It does not enforce capacity, and nothing reads it for admission yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from gateway.arbitration.holdings import SettlementKind
from gateway.arbitration.types import ResourceRef

SCHEMA_VERSION = 2
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = _REPO_ROOT / "data" / "arbitration.db"
# A lease must not wait long on the store. One short busy wait, no retries.
DEFAULT_BUSY_TIMEOUT_S = 0.05
READ_BUSY_TIMEOUT_S = 5.0
REQUIRED_TABLES = frozenset({"resource_generations", "operations", "comparisons"})

OPEN_STATES = ("committed", "reconciling")

ACQUIRED = "committed"
REPLAYED = "replayed"
PAYLOAD_MISMATCH = "payload_mismatch"
STALE_GENERATION = "stale_generation"
UNISSUED_GENERATION = "unissued_generation"
UNIT_MISMATCH = "unit_mismatch"

RELEASED = "released"
ENDED_RECORDED = "execution_recorded"
UNCHANGED = "unchanged"
NOT_RELEASED = "not_released"
UNKNOWN_OPERATION = "unknown_operation"
INVALID_EVIDENCE = "invalid_evidence"  # a kind that is neither STOCK_RELEASED nor EXECUTION_ENDED


class StoreSchemaError(RuntimeError):
    """The file has a schema version this code does not read, or it is not a shadow ledger."""


def default_path() -> Path:
    """AZIZAM_ARBITRATION_DB when set, else data/arbitration.db in the repository."""
    configured = os.environ.get("AZIZAM_ARBITRATION_DB")
    return Path(configured) if configured else DEFAULT_PATH


@dataclass(frozen=True)
class Operation:
    op_key: str
    resource_id: str
    incarnation: str
    generation: int
    quantity: int
    unit: str
    state: str
    execution_ended: bool = field(default=False)


@dataclass(frozen=True)
class Acquired:
    """The first result for an operation key. `status` is one of the acquire constants above."""

    status: str
    operation: Operation | None


@dataclass(frozen=True)
class LedgerSnapshot:
    """One read transaction's view of a ledger: its schema version, comparison counts and open operations."""

    schema_version: int
    comparisons: dict[str, int]
    open_operations: list[Operation]


class ArbitrationStore:
    def __init__(self, path: str | os.PathLike[str], *, busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=busy_timeout_s, isolation_level=None,
                                     check_same_thread=False)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._open_schema()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- schema -------------------------------------------------------------------------------

    def _open_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise StoreSchemaError(f"arbitration store {self.path} has schema {version}; this code reads {SCHEMA_VERSION}")
        self._write(lambda c: self._create_schema(c))

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.execute("""CREATE TABLE IF NOT EXISTS resource_generations (
                            resource_id TEXT PRIMARY KEY,
                            generation INTEGER NOT NULL CHECK (generation >= 1))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS operations (
                            op_key TEXT PRIMARY KEY,
                            resource_id TEXT NOT NULL,
                            incarnation TEXT NOT NULL,
                            generation INTEGER NOT NULL CHECK (generation >= 1),
                            quantity INTEGER NOT NULL CHECK (quantity >= 0),
                            unit TEXT NOT NULL,
                            payload TEXT NOT NULL,
                            state TEXT NOT NULL CHECK (state IN ('committed', 'reconciling', 'settled')),
                            execution_ended INTEGER NOT NULL DEFAULT 0 CHECK (execution_ended IN (0, 1)),
                            settle_source TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS operations_by_resource ON operations (resource_id, state)")
        conn.execute("""CREATE TABLE IF NOT EXISTS comparisons (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            attempt_key TEXT NOT NULL,
                            status TEXT NOT NULL,
                            old_admits INTEGER,
                            new_outcome TEXT,
                            open_holdings INTEGER,
                            detail TEXT NOT NULL)""")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # ---- transactions -------------------------------------------------------------------------

    def _write(self, action):
        """Run `action(conn)` and COMMIT inside one BEGIN IMMEDIATE transaction. Roll back on any error."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = action(conn)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return result

    # ---- fencing ------------------------------------------------------------------------------

    def take_over(self, resource_id: str) -> int:
        """Start a new generation for a resource. Older open operations move to reconciling and stay charged."""
        _require_text("resource_id", resource_id)

        def action(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT generation FROM resource_generations WHERE resource_id = ?",
                               (resource_id,)).fetchone()
            new = (row[0] if row else 0) + 1
            conn.execute("INSERT INTO resource_generations (resource_id, generation) VALUES (?, ?) "
                         "ON CONFLICT (resource_id) DO UPDATE SET generation = excluded.generation",
                         (resource_id, new))
            conn.execute("UPDATE operations SET state = 'reconciling' "
                         "WHERE resource_id = ? AND generation < ? AND state = 'committed'",
                         (resource_id, new))
            return new

        with self._lock:
            return self._write(action)

    def generation(self, resource_id: str) -> int:
        with self._lock:
            return _generation(self._conn, resource_id)

    # ---- operations ---------------------------------------------------------------------------

    def acquire(self, *, op_key: str, ref: ResourceRef, quantity: int, unit: str, generation: int) -> Acquired:
        """Record one operation with its reservation. A replay returns the stored operation as it is now (not a copy of the first response); a stale generation is refused.

        A refused generation or unit writes nothing, so a later acquire with a current generation may still commit.
        """
        _require_text("op_key", op_key)
        _require_text("unit", unit)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise ValueError("quantity must be a non-negative int")
        payload = _payload(ref, quantity, unit)

        def action(conn: sqlite3.Connection) -> Acquired:
            row = _fetch_operation(conn, op_key)
            if row is not None:
                if row[-1] != payload:
                    return Acquired(PAYLOAD_MISMATCH, _operation(row))
                return Acquired(REPLAYED, _operation(row))
            current = _generation(conn, ref.id)
            if generation < current:
                return Acquired(STALE_GENERATION, None)
            if generation != current:
                return Acquired(UNISSUED_GENERATION, None)
            mixed = conn.execute("SELECT 1 FROM operations WHERE resource_id = ? AND incarnation = ? "
                                 "AND state != 'settled' AND unit != ? LIMIT 1",
                                 (ref.id, ref.incarnation, unit)).fetchone()
            if mixed is not None:
                return Acquired(UNIT_MISMATCH, None)
            conn.execute("INSERT INTO operations (op_key, resource_id, incarnation, generation, quantity, unit, "
                         "payload, state, execution_ended, settle_source) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, 'committed', 0, NULL)",
                         (op_key, ref.id, ref.incarnation, generation, quantity, unit, payload))
            return Acquired(ACQUIRED, Operation(op_key, ref.id, ref.incarnation, generation, quantity, unit,
                                                "committed"))

        with self._lock:
            return self._write(action)

    def settle(self, *, op_key: str, ref: ResourceRef, kind: SettlementKind, source: str) -> str:
        """Release only on STOCK_RELEASED evidence for this resource and operation. Returns a settle constant.

        EXECUTION_ENDED records that the execution ended and keeps the charge. It never releases.
        """
        _require_text("source", source)

        def action(conn: sqlite3.Connection) -> str:
            row = conn.execute("SELECT resource_id, incarnation, state, execution_ended FROM operations "
                               "WHERE op_key = ?", (op_key,)).fetchone()
            if row is None:
                return UNKNOWN_OPERATION
            resource_id, incarnation, state, ended = row
            if state == "settled":
                return UNCHANGED
            if (resource_id, incarnation) != (ref.id, ref.incarnation):
                return NOT_RELEASED  # evidence about another resource releases nothing
            if kind is SettlementKind.EXECUTION_ENDED:
                if ended:
                    return UNCHANGED
                conn.execute("UPDATE operations SET execution_ended = 1 WHERE op_key = ?", (op_key,))
                return ENDED_RECORDED
            if kind is not SettlementKind.STOCK_RELEASED:
                return INVALID_EVIDENCE  # fail closed: only stock-release evidence releases
            conn.execute("UPDATE operations SET state = 'settled', settle_source = ? WHERE op_key = ?",
                         (source, op_key))
            return RELEASED

        with self._lock:
            return self._write(action)

    def charge(self, ref: ResourceRef) -> int:
        """Total quantity on one resource incarnation that is not settled, whatever its state."""
        with self._lock:
            rows = self._conn.execute("SELECT quantity FROM operations WHERE resource_id = ? AND incarnation = ? "
                                      "AND state != 'settled'", (ref.id, ref.incarnation)).fetchall()
        return sum(row[0] for row in rows)

    def open_holdings(self, ref: ResourceRef) -> int:
        """How many operations on one resource incarnation are not settled."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM operations WHERE resource_id = ? AND incarnation = ? "
                                     "AND state != 'settled'", (ref.id, ref.incarnation)).fetchone()
        return row[0]

    def operation(self, op_key: str) -> Operation | None:
        with self._lock:
            row = _fetch_operation(self._conn, op_key)
        return None if row is None else _operation(row)

    def open_operations(self) -> list[Operation]:
        with self._lock:
            rows = self._conn.execute(_OPERATION_SELECT + " WHERE state != 'settled' ORDER BY op_key").fetchall()
        return [_operation(row) for row in rows]

    # ---- shadow comparisons -------------------------------------------------------------------

    def record_comparison(self, *, attempt_key: str, status: str, old_admits: bool | None,
                          new_outcome: str | None, detail: str, open_holdings: int | None = None) -> None:
        """Store one comparison. `open_holdings` is how many holdings of the pool were unsettled at admission."""
        _require_text("attempt_key", attempt_key)

        def action(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO comparisons (attempt_key, status, old_admits, new_outcome, open_holdings, detail) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (attempt_key, status, None if old_admits is None else int(old_admits), new_outcome,
                          open_holdings, detail))

        with self._lock:
            self._write(action)

    def comparison_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) FROM comparisons GROUP BY status ORDER BY status").fetchall()
        return {status: count for status, count in rows}

    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]


def read_snapshot(path: str | os.PathLike[str]) -> LedgerSnapshot:
    """Read an existing ledger in one read transaction. The file is opened read-only and is never created or migrated.

    Raises FileNotFoundError for a missing file and StoreSchemaError for a file that is not a ledger of this schema.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no shadow ledger at {path}")
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=READ_BUSY_TIMEOUT_S, isolation_level=None)
    try:
        try:
            conn.execute("BEGIN")
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if version != SCHEMA_VERSION or not REQUIRED_TABLES <= tables:
                    raise StoreSchemaError(f"{path} is not a shadow ledger of schema {SCHEMA_VERSION} "
                                           f"(user_version {version})")
                counts = {status: count for status, count in conn.execute(
                    "SELECT status, COUNT(*) FROM comparisons GROUP BY status ORDER BY status").fetchall()}
                operations = [_operation(row) for row in conn.execute(
                    _OPERATION_SELECT + " WHERE state != 'settled' ORDER BY op_key").fetchall()]
                return LedgerSnapshot(version, counts, operations)
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        except sqlite3.DatabaseError as error:
            raise StoreSchemaError(f"{path} is not a readable shadow ledger: {error}") from error
    finally:
        conn.close()


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _payload(ref: ResourceRef, quantity: int, unit: str) -> str:
    """Identity of an operation's ask. The generation is a fence, not part of the identity."""
    body = json.dumps({"resource": ref.id, "incarnation": ref.incarnation, "quantity": quantity, "unit": unit},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _generation(conn: sqlite3.Connection, resource_id: str) -> int:
    row = conn.execute("SELECT generation FROM resource_generations WHERE resource_id = ?", (resource_id,)).fetchone()
    return row[0] if row else 0


_OPERATION_SELECT = ("SELECT op_key, resource_id, incarnation, generation, quantity, unit, state, execution_ended, "
                     "payload FROM operations")


def _fetch_operation(conn: sqlite3.Connection, op_key: str) -> tuple[Any, ...] | None:
    return conn.execute(_OPERATION_SELECT + " WHERE op_key = ?", (op_key,)).fetchone()


def _operation(row: tuple[Any, ...]) -> Operation:
    """Build an Operation from a row whose first eight columns are the public fields (payload may follow)."""
    op_key, resource_id, incarnation, generation, quantity, unit, state, ended = row[:8]
    return Operation(op_key, resource_id, incarnation, generation, quantity, unit, state, bool(ended))

"""Owner-installed protocol descriptions: rows in the admin database, one revision counter for all of them.

A description the owner installs through the engine protocols route is stored here as the raw document. The dispatch
loader (``gateway/engine_protocol/description.py``) merges these rows with the shipped files and the overlay folder. A
second gateway worker needs no restart: every write moves the counter ``protocol_descriptions`` in
``operator_settings_generations``, the loader reads that one number on each call and parses the rows again only when it
changed. The counter never goes down, so a row revision is never reused, not even after a delete and a create.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from gateway import admin_store

GENERATION_KEY = "protocol_descriptions"


class StoreConflict(Exception):
    """A write the stored state refuses. ``kind`` is ``exists``, ``missing``, ``stale`` or ``referenced``."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind, self.detail = kind, detail


@dataclass(frozen=True)
class Row:
    id: str
    text: str
    revision: int


def _generation_in(conn: sqlite3.Connection) -> int:
    found = conn.execute(
        "SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key=?", (GENERATION_KEY,)).fetchone()
    return int(found[0]) if found else 0


def generation(path: Path | None = None) -> int:
    """The counter of the stored descriptions; ``0`` when the database or the table does not exist yet."""
    path = path or admin_store.DB_PATH
    if not path.is_file():
        return 0
    try:
        with admin_store.shared_connection(path) as conn:
            return _generation_in(conn)
    except sqlite3.OperationalError:
        return 0


def read_all(path: Path | None = None) -> tuple[int, list[Row]]:
    """The counter and every row from one snapshot."""
    path = path or admin_store.DB_PATH
    if not path.is_file():
        return 0, []
    try:
        with admin_store.shared_connection(path) as conn:
            started = not conn.in_transaction
            if started:
                conn.execute("BEGIN")
            try:
                rows = [Row(str(r[0]), str(r[1]), int(r[2])) for r in conn.execute(
                    "SELECT id, document_text, revision FROM protocol_descriptions ORDER BY id")]
                return _generation_in(conn), rows
            finally:
                if started:
                    conn.rollback()
    except sqlite3.OperationalError:
        return 0, []


def get(protocol_id: str, path: Path | None = None) -> Row | None:
    return next((row for row in read_all(path)[1] if row.id == protocol_id), None)


def _naming(registry: Any, protocol_id: str) -> list[str]:
    """The ids of the engines in a stored registry document whose connection names ``protocol_id``."""
    services = registry.get("services") if isinstance(registry, Mapping) else None
    found = []
    for service_id, record in (services.items() if isinstance(services, Mapping) else ()):
        if not isinstance(record, Mapping):
            continue
        config = record.get("config") if isinstance(record.get("config"), Mapping) else {}
        for connection in (record.get("connection"), config.get("connection")):
            if isinstance(connection, Mapping) and connection.get("protocol") == protocol_id:
                found.append(str(service_id))
                break
    return sorted(found)


def put(protocol_id: str, text: str, *, expect: int | None, updated_by: str | None = None, path: Path | None = None) -> int:
    """Create (``expect`` None) or replace (``expect`` the current revision) one row; the new revision.

    Raises ``StoreConflict``: ``exists`` when a create finds the row, ``missing`` when a replace finds none, ``stale``
    when the revision moved. The check and the write share one ``BEGIN IMMEDIATE`` transaction, so two writers cannot both win."""
    with admin_store.transaction(path or admin_store.DB_PATH) as tx:
        conn = tx.conn
        current = conn.execute("SELECT revision FROM protocol_descriptions WHERE id=?", (protocol_id,)).fetchone()
        if expect is None and current is not None:
            raise StoreConflict("exists")
        if expect is not None and current is None:
            raise StoreConflict("missing")
        if expect is not None and int(current[0]) != expect:
            raise StoreConflict("stale")
        revision = _generation_in(conn) + 1
        conn.execute(
            "INSERT INTO protocol_descriptions(id, document_text, revision, updated_at, updated_by) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET document_text=excluded.document_text, revision=excluded.revision, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (protocol_id, text, revision, int(time.time()), updated_by))
        _set_generation(conn, revision)
        return revision


def _set_generation(conn: sqlite3.Connection, value: int) -> None:
    conn.execute(
        "INSERT INTO operator_settings_generations(setting_key, operator_settings_generation) VALUES (?, ?) "
        "ON CONFLICT(setting_key) DO UPDATE SET operator_settings_generation=excluded.operator_settings_generation",
        (GENERATION_KEY, value))


def delete(protocol_id: str, *, expect: int, path: Path | None = None) -> None:
    """Delete one row at its revision. ``referenced`` when an engine names it (``detail`` lists the engine ids)."""
    with admin_store.transaction(path or admin_store.DB_PATH) as tx:
        conn = tx.conn
        current = conn.execute("SELECT revision FROM protocol_descriptions WHERE id=?", (protocol_id,)).fetchone()
        if current is None:
            raise StoreConflict("missing")
        if int(current[0]) != expect:
            raise StoreConflict("stale")
        stored = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()
        try:
            registry = json.loads(stored[0]) if stored else {}
        except ValueError:
            registry = {}
        naming = _naming(registry, protocol_id)
        if naming:
            raise StoreConflict("referenced", ", ".join(naming))
        conn.execute("DELETE FROM protocol_descriptions WHERE id=?", (protocol_id,))
        _set_generation(conn, _generation_in(conn) + 1)

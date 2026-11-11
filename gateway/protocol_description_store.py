"""Owner-installed protocol descriptions: rows in the admin database, one revision counter for all of them.

A description the owner installs through the engine protocols route is stored here as the raw document. The dispatch
loader (``gateway/engine_protocol/description.py``) merges these rows with the shipped files and the overlay folder. The
gateway's single process invalidates its dispatch cache after each committed write. Standalone readers still check the
stored counter, so administrative scripts do not keep stale snapshots. The counter never goes down, so a row revision
is never reused, not even after a delete and a create.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Mapping

from gateway import admin_store

GENERATION_KEY = "protocol_descriptions"

# Where the running gateway keeps its admin database. proxy.py registers its store here; a bare process (a script, a
# second worker's loader before the app exists) reads the default path.
_provider: Callable[[], Path] | None = None
_cache_revisions: dict[Path, int] = {}


def use_path(provider: Callable[[], Path] | None) -> None:
    """Register the single gateway instance; its description writes all use this store and invalidate after commit."""
    global _provider
    _provider = provider


def db_path() -> Path:
    return Path(_provider()) if _provider is not None else Path(admin_store.DB_PATH)


def cache_revision(path: Path) -> int:
    """No database access in the running gateway; standalone readers check the persisted revision."""
    return _cache_revisions.get(path, 0) if _provider is not None else generation(path)


def _invalidate(path: Path) -> None:
    _cache_revisions[path] = _cache_revisions.get(path, 0) + 1


class StoreConflict(Exception):
    """A write the stored state refuses. ``kind`` is ``exists``, ``missing``, ``stale``, ``referenced`` or ``operations``
    (a replacement drops an operation an engine still advertises; ``detail`` names the engines and the operations)."""

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


def _table_absent(error: sqlite3.OperationalError) -> bool:
    """True only for SQLite's answer that a table does not exist. A lock, a disk error or a corrupt file is not absence."""
    return "no such table" in str(error).lower()


def generation(path: Path | None = None) -> int:
    """The counter of the stored descriptions; ``0`` only when the database or the table is verifiably absent.

    Any other ``sqlite3`` error is raised: a read that failed says nothing about what is installed."""
    path = path or db_path()
    if not path.is_file():
        return 0
    try:
        with admin_store.shared_connection(path) as conn:
            return _generation_in(conn)
    except sqlite3.OperationalError as error:
        if _table_absent(error):
            return 0
        raise


def read_all(path: Path | None = None) -> tuple[int, list[Row]]:
    """The counter and every row from one snapshot; empty only when the database or the table is verifiably absent.

    Any other ``sqlite3`` error is raised, so a caller never takes a failed read for "nothing installed"."""
    path = path or db_path()
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
    except sqlite3.OperationalError as error:
        if _table_absent(error):
            return 0, []
        raise


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


def _stored_registry(conn: sqlite3.Connection) -> Any:
    stored = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()
    try:
        return json.loads(stored[0]) if stored else {}
    except ValueError:
        return {}


def _advertising(registry: Any, protocol_id: str, operations: Collection[str]) -> dict[str, list[str]]:
    """Per engine that names ``protocol_id``, the ``operations`` its record still advertises as capabilities."""
    wanted = set(operations)
    services = registry.get("services") if isinstance(registry, Mapping) else None
    found: dict[str, list[str]] = {}
    for service_id in _naming(registry, protocol_id):
        record = services[service_id]
        config = record.get("config") if isinstance(record.get("config"), Mapping) else {}
        advertised = {c for source in (record.get("capabilities"), config.get("capabilities"))
                      if isinstance(source, (list, tuple)) for c in source if isinstance(c, str)}
        if advertised & wanted:
            found[service_id] = sorted(advertised & wanted)
    return found


def _operations_of(text: str, protocol_id: str) -> set[str]:
    from gateway.engine_protocol import description

    try:
        return set(description.parse_description_text(text, source=protocol_id).operations)
    except description.DescriptionError:
        return set()


def put(protocol_id: str, text: str, *, expect: int | None, updated_by: str | None = None, path: Path | None = None,
        declares: Iterable[str] | None = None) -> int:
    """Create (``expect`` None) or replace (``expect`` the current revision) one row; the new revision.

    Raises ``StoreConflict``: ``exists`` when a create finds the row, ``missing`` when a replace finds none, ``stale``
    when the revision moved. The check and the write share one ``BEGIN IMMEDIATE`` transaction, so two writers cannot both win.

    A replace names the operations the new document ``declares``. It is refused (``operations``) when an engine that names
    this description still advertises an operation the stored document declared and the new one does not; the engine
    writes recheck the description under the same writer lock, so the two cannot interleave."""
    with admin_store.transaction(path or db_path()) as tx:
        conn = tx.conn
        current = conn.execute("SELECT revision, document_text FROM protocol_descriptions WHERE id=?", (protocol_id,)).fetchone()
        if expect is None and current is not None:
            raise StoreConflict("exists")
        if expect is not None and current is None:
            raise StoreConflict("missing")
        if expect is not None and int(current[0]) != expect:
            raise StoreConflict("stale")
        if current is not None and declares is not None:
            removed = _operations_of(str(current[1]), protocol_id) - set(declares)
            clash = _advertising(_stored_registry(conn), protocol_id, removed) if removed else {}
            if clash:
                raise StoreConflict("operations", "; ".join(f"{engine} ({', '.join(ops)})" for engine, ops in sorted(clash.items())))
        revision = _generation_in(conn) + 1
        conn.execute(
            "INSERT INTO protocol_descriptions(id, document_text, revision, updated_at, updated_by) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET document_text=excluded.document_text, revision=excluded.revision, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (protocol_id, text, revision, int(time.time()), updated_by))
        _set_generation(conn, revision)
        tx.after_commit(lambda: _invalidate(path or db_path()))
        return revision


def _set_generation(conn: sqlite3.Connection, value: int) -> None:
    conn.execute(
        "INSERT INTO operator_settings_generations(setting_key, operator_settings_generation) VALUES (?, ?) "
        "ON CONFLICT(setting_key) DO UPDATE SET operator_settings_generation=excluded.operator_settings_generation",
        (GENERATION_KEY, value))


def delete(protocol_id: str, *, expect: int, path: Path | None = None) -> None:
    """Delete one row at its revision. ``referenced`` when an engine names it (``detail`` lists the engine ids)."""
    with admin_store.transaction(path or db_path()) as tx:
        conn = tx.conn
        current = conn.execute("SELECT revision FROM protocol_descriptions WHERE id=?", (protocol_id,)).fetchone()
        if current is None:
            raise StoreConflict("missing")
        if int(current[0]) != expect:
            raise StoreConflict("stale")
        naming = _naming(_stored_registry(conn), protocol_id)
        if naming:
            raise StoreConflict("referenced", ", ".join(naming))
        conn.execute("DELETE FROM protocol_descriptions WHERE id=?", (protocol_id,))
        _set_generation(conn, _generation_in(conn) + 1)
        tx.after_commit(lambda: _invalidate(path or db_path()))


class DescriptionRefused(ValueError):
    """An engine write names a description that is gone, or an operation the description no longer declares.

    The message is a fixed sentence: it never carries a stored or posted text."""


def declared_operations(conn: sqlite3.Connection, protocol_id: str) -> set[str] | None:
    """The operations a dispatching engine may use from ``protocol_id``, read inside the caller's write transaction.

    ``None`` when no description of that id resolves (or an owner row no longer parses). The owner row wins over the files,
    as in the loader; it is read from ``conn``, so the answer is the state this transaction will commit on."""
    from gateway.engine_protocol import description

    try:
        row = conn.execute("SELECT document_text FROM protocol_descriptions WHERE id=?", (protocol_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if not _table_absent(error):
            raise
        row = None
    if row is not None:
        try:
            doc = description.parse_description_text(str(row[0]), source=protocol_id)
        except description.DescriptionError:
            return None
    else:
        doc = description.load_registry().get(protocol_id)
    if doc is None:
        return None
    return set(doc.operations) if doc.executor == "http-v1" else set()


def require_operations(conn: sqlite3.Connection, protocol_id: str, operations: Collection[str]) -> None:
    """Refuse (``DescriptionRefused``) unless ``protocol_id`` exists now and declares every one of ``operations``."""
    declared = declared_operations(conn, protocol_id)
    if declared is None:
        raise DescriptionRefused("The protocol description this engine uses is no longer installed. Choose another one.")
    if set(operations) - declared:
        raise DescriptionRefused("The protocol description no longer declares every task this engine offers. Read it again.")


def _dispatching(registry: Any) -> dict[str, str]:
    services = registry.get("services") if isinstance(registry, Mapping) else None
    found: dict[str, str] = {}
    for service_id, record in (services.items() if isinstance(services, Mapping) else ()):
        if not isinstance(record, Mapping):
            continue
        config = record.get("config") if isinstance(record.get("config"), Mapping) else {}
        for connection in (record.get("connection"), config.get("connection")):
            if isinstance(connection, Mapping) and connection.get("dispatch") == "protocol" and isinstance(connection.get("protocol"), str):
                found[str(service_id)] = connection["protocol"]
                break
    return found


def require_new_references(conn: sqlite3.Connection, before: Any, after: Any) -> None:
    """Called by every registry write inside its transaction: an engine that now names a description it did not name
    before needs that description to exist. A reference the registry already held does not stop an unrelated write."""
    held = _dispatching(before)
    for service_id, protocol_id in _dispatching(after).items():
        if held.get(service_id) != protocol_id and declared_operations(conn, protocol_id) is None:
            raise DescriptionRefused("An engine names a protocol description that is not installed. Install it first.")

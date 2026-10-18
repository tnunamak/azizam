"""Admin schema 11: move every inline secret out of stored records into the encrypted store (decision D26).

One idempotent pass over the stored service registry and the extension package index. A header value, an
environment value, a command argument after a credential flag and the user info and query of a URL become
secret entries (``gateway/record_carriers.py``); the record keeps references and public metadata. A registry that
holds no inline secret is not touched: the pass writes nothing, bumps no generation and records no audit entry.
Nothing here decrypts a value, and the audit entry holds counts only.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from gateway import record_carriers, secret_refs, secret_store

REGISTRY_KEY = "service_registry"
PACKAGE_INDEX_KEY = "extension_package_index"


def _database_path(conn: sqlite3.Connection) -> Path:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file)
    raise sqlite3.DatabaseError("the admin database has no file path")


def _load(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (key,)).fetchone()
    return None if row is None else json.loads(row[0])


def _store(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("UPDATE operator_settings SET value_json=? WHERE key=?", (json.dumps(value), key))
    conn.execute(
        "UPDATE operator_settings_generations SET operator_settings_generation=operator_settings_generation+1 "
        "WHERE setting_key=?", (key,))


def migrate(conn: sqlite3.Connection) -> dict[str, int]:
    """Externalize both stores inside ``conn``'s transaction. Returns ``{store: entries moved}``."""
    db_path = _database_path(conn)
    stored = {"puts": 0}

    def counting_put(inner):
        def put(value: str, origin: str | None = None) -> str:
            stored["puts"] += 1
            return inner(value, origin)
        return put

    put = counting_put(secret_refs.putter(db_path, conn))
    changed: dict[str, int] = {}

    registry = _load(conn, REGISTRY_KEY)
    if isinstance(registry, dict) and isinstance(registry.get("services"), dict):
        moved_total = 0
        for service_id, record in list(registry["services"].items()):
            if not isinstance(record, dict):
                continue
            new, moved = record_carriers.externalize_service(record, put)
            if moved:
                registry["services"][service_id] = new
                moved_total += len(moved)
        if moved_total:
            _store(conn, REGISTRY_KEY, registry)
            changed["registry"] = moved_total

    index = _load(conn, PACKAGE_INDEX_KEY)
    if isinstance(index, dict):
        new_index, moved = record_carriers.externalize_package_index(index, put)
        if moved:
            _store(conn, PACKAGE_INDEX_KEY, new_index)
            changed["package_index"] = len(moved)

    if changed:
        from gateway import admin_store

        admin_store._record_audit_in_transaction(
            conn, "operator.secrets.externalize", actor_kind="system", actor_id="migration",
            actor_label="Schema 11 migration", target_kind="operator_setting", target_id=REGISTRY_KEY,
            metadata=dict(changed), now=int(time.time()),
        )
    return changed

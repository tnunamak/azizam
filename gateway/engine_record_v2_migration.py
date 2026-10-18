"""One-time admin schema 10 migration for the engine record v2 view."""

from __future__ import annotations

import json
import sqlite3
import time

from gateway import service_registry


def migrate_registry(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()
    if row is None:
        return []
    registry = json.loads(row[0])
    changed = service_registry.migrate_engine_records_v2(registry)
    if not changed:
        return []
    conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(registry),))
    conn.execute(
        "UPDATE operator_settings_generations SET operator_settings_generation=operator_settings_generation+1 "
        "WHERE setting_key='service_registry'"
    )
    from gateway import admin_store

    admin_store._record_audit_in_transaction(
        conn, "operator.engine_record_v2.migrate", actor_kind="system", actor_id="migration",
        actor_label="Schema 10 migration", target_kind="operator_setting",
        target_id="service_registry", metadata={"service_count": len(changed)}, now=int(time.time()),
    )
    return changed

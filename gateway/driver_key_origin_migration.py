"""One-time data migration (admin schema version 12): record where each lifecycle driver key goes.

Before version 12 an HTTP load/unload driver sent its admin key and API key to the host and port of its record, with
nothing recording which address the owner meant. It now sends them only to the origin recorded beside them
(``config.driver_credential_origin``), so that a record whose host moved past the owner-only routes cannot redirect
them.

Every record that exists when this runs was configured by the owner (an administrator could neither attach a driver
key nor, by design, move the host of a record that holds one, except through the gap this version closes), so the
address it dials today is trusted: for each record whose lifecycle names a key and that records no origin, record the
origin of the address its driver dials.
"""
from __future__ import annotations

import json
import sqlite3
import time

from gateway import service_registry

AUDIT_ACTION = "operator.driver_key_origin.migrate"


def migrate_registry(conn: sqlite3.Connection) -> list[str]:
    """Record the origin inside the caller's transaction; return the service ids changed."""
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()
    if row is None:
        return []
    registry = json.loads(row[0])
    changed = []
    for service_id, service in sorted((registry.get("services") or {}).items()):
        config = service.get("config") if isinstance(service, dict) else None
        if not isinstance(config, dict) or config.get(service_registry.DRIVER_CREDENTIAL_ORIGIN_KEY):
            continue
        if not service_registry.driver_keys_held(service):
            continue
        origin = service_registry.origin_of(service_registry.lifecycle_dial_url(config))
        if not origin:
            continue
        config[service_registry.DRIVER_CREDENTIAL_ORIGIN_KEY] = origin
        changed.append(service_id)
    if not changed:
        return []
    conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(registry),))
    conn.execute(
        "UPDATE operator_settings_generations "
        "SET operator_settings_generation=operator_settings_generation+1 WHERE setting_key='service_registry'")
    from gateway import admin_store

    admin_store._record_audit_in_transaction(
        conn, AUDIT_ACTION,
        actor_kind="system", actor_id="migration", actor_label="Schema 12 migration",
        target_kind="operator_setting", target_id="service_registry",
        metadata={"services": changed}, now=int(time.time()),
    )
    return changed

"""One-time data migration (admin schema version 8): record where each credential goes.

Before version 8 the compiler sent a service record's credential to whatever
host the record named. It now sends a local record's key only to the origin
recorded with the credential (``config.credential_origin``), so that a record
written past the owner-only routes cannot redirect a key.

Every record that exists when this runs was configured by the owner, so its
current destination is trusted: for each record that names a credential and
records no origin, record the origin of its current base URL. A later edit by
an administrator cannot add a credential or move such a record, and gets no
origin recorded.
"""
from __future__ import annotations

import json
import sqlite3
import time

from gateway import service_registry

AUDIT_ACTION = "operator.credential_origin.migrate"


def _destination(config: dict) -> str:
    """The origin the record's credential is sent to today."""
    return service_registry.origin_of(
        str(config.get(service_registry.LIFECYCLE_BASE_KEY) or "") or service_registry.service_base_url(config)
    )


def migrate_registry(conn: sqlite3.Connection) -> list[str]:
    """Record the origin inside the caller's transaction; return the service ids changed."""
    row = conn.execute(
        "SELECT value_json FROM operator_settings WHERE key='service_registry'"
    ).fetchone()
    if row is None:
        return []
    registry = json.loads(row[0])
    changed = []
    for service_id, service in sorted((registry.get("services") or {}).items()):
        config = service.get("config") if isinstance(service, dict) else None
        if not isinstance(config, dict):
            continue
        if not any(config.get(key) for key in service_registry.CREDENTIAL_REFERENCE_KEYS):
            continue
        if config.get("credential_origin"):
            continue
        origin = _destination(config)
        if not origin:
            continue
        config["credential_origin"] = origin
        changed.append(service_id)
    if not changed:
        return []
    conn.execute(
        "UPDATE operator_settings SET value_json=? WHERE key='service_registry'",
        (json.dumps(registry),),
    )
    conn.execute(
        "UPDATE operator_settings_generations "
        "SET operator_settings_generation=operator_settings_generation+1 "
        "WHERE setting_key='service_registry'"
    )
    from gateway import admin_store

    admin_store._record_audit_in_transaction(
        conn, AUDIT_ACTION,
        actor_kind="system", actor_id="migration", actor_label="Schema 8 migration",
        target_kind="operator_setting", target_id="service_registry",
        metadata={"services": changed}, now=int(time.time()),
    )
    return changed

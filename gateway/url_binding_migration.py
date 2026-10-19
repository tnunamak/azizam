"""One-time data migration (admin schema version 13): report the URL credentials that are not bound to an address.

Before this version the credentials stored with a URL field (``<field>_entries``) were bound to the origin of the
field's own text, so a path (``/ready?token=...``) held its credentials bound to nothing. Decision D42 says what to do
with them: nothing. The migration does **not** bind them. A record that exists when this runs may have been changed by an
administrator under an older version (an administrator could re-aim such a field without the owner-only rule noticing),
so the address it has today is not evidence of what the owner meant. The runtime sends a credential that is bound to no
origin nowhere, and the engine view says the owner must confirm the address. Binding happens only on an owner write:
re-entering the credentials, or the owner-only ``confirm_address`` command, which binds them to the address the field has
at that moment.

This pass therefore changes no secret and no record. It counts the credentials of URL fields that are bound to no
origin and, when there are any, writes one audit entry with the service ids and the count (never a value or a
reference), so the owner can see which engines need a confirmation. A database with none writes nothing.
"""
from __future__ import annotations

import json
import sqlite3
import time

from gateway import record_carriers

AUDIT_ACTION = "operator.url_binding.migrate"


def migrate_registry(conn: sqlite3.Connection) -> dict[str, object]:
    """Count the unbound URL credentials inside the caller's transaction; return the count and the service ids."""
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()
    if row is None:
        return {}
    registry = json.loads(row[0])
    has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_secrets'").fetchone()
    unconfirmed: set[str] = set()
    services: set[str] = set()
    for service_id, record in sorted((registry.get("services") or {}).items()):
        if not isinstance(record, dict):
            continue
        for path, parent, key in record_carriers.url_containers(record):
            held = parent.get(key + record_carriers.SECRETS_SUFFIX)
            if not isinstance(held, dict) or not held:
                continue
            for ref in record_carriers.refs_in(held):
                origin = conn.execute("SELECT approved_origin FROM provider_secrets WHERE id=?", (ref,)).fetchone() if has_table else None
                if origin is None or origin[0] is None:
                    unconfirmed.add(ref)  # a probe path copies the driver's entries: count each credential once
                    services.add(service_id)
    if not unconfirmed:
        return {}
    print(f"[migration 13] {len(unconfirmed)} stored URL credential(s) of {len(services)} service(s) are bound to no address and "
          f"are not sent until the owner confirms it: {sorted(services)}", flush=True)
    from gateway import admin_store

    admin_store._record_audit_in_transaction(
        conn, AUDIT_ACTION,
        actor_kind="system", actor_id="migration", actor_label="Schema 13 migration",
        target_kind="operator_setting", target_id="service_registry",
        metadata={"services": sorted(services), "unconfirmed": len(unconfirmed)},
        now=int(time.time()),
    )
    return {"unconfirmed": len(unconfirmed), "services": sorted(services)}

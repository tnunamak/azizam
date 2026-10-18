"""One-time data migration (admin schema version 13): bind the credentials stored with a URL to its effective origin.

Before version 13 the credentials of a URL field (``<field>_entries``) were bound to the origin of the field's own text,
so a path (``/ready?token=...``) held its credentials with no origin at all and an administrator who changed the field to
``//other.host/x`` could send them there (decision D39). Now each is bound to the effective origin of its field: the
field's own when it is an absolute http(s) URL, else the origin of the base it is joined to (``url_binding.base_of``).

Every record that exists when this runs was written by the owner (only the owner stores a credential), so the address it
reaches today is trusted. For each field that holds credentials, each stored secret is bound to that origin (encrypted
again under the new binding; the value is read inside the transaction and never leaves it). A field that has no origin
(a protocol-relative address, a path with no base) is left as it is: the runtime never puts its credentials back. The
pass is idempotent: a secret already bound to the origin is not touched, and a database with nothing to bind writes
nothing, bumps no generation and records no audit entry. The audit entry holds counts and service ids only.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from gateway import record_carriers, secret_refs, secret_store, url_binding

AUDIT_ACTION = "operator.url_binding.migrate"


def _database_path(conn: sqlite3.Connection) -> Path:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file)
    raise sqlite3.DatabaseError("the admin database has no file path")


def migrate_registry(conn: sqlite3.Connection) -> dict[str, object]:
    """Bind the stored URL credentials inside the caller's transaction; return the counts and the service ids changed."""
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()
    if row is None:
        return {}
    registry = json.loads(row[0])
    db_path = _database_path(conn)
    rebound, unbound, unreadable = 0, 0, 0
    services: set[str] = set()
    host_userinfo: set[str] = set()
    for service_id, record in sorted((registry.get("services") or {}).items()):
        if not isinstance(record, dict):
            continue
        for path, parent, key in record_carriers.url_containers(record):
            held = parent.get(key + record_carriers.SECRETS_SUFFIX)
            if not isinstance(held, dict) or not held:
                continue
            if path == "config.host" and held.get("userinfo"):
                host_userinfo.add(service_id)
            secret_store.ensure_provider_secret_schema_on(conn)
            refs = record_carriers.refs_in(held)
            base = url_binding.base_of(record, path)
            try:
                if isinstance(held.get("whole"), dict):
                    value, _ = secret_store.read_provider_secret_on(conn, held["whole"].get("secret_ref"), db_path=db_path)
                    target = url_binding.field_origin(value, base)
                else:
                    target = url_binding.field_origin(parent.get(key), base)
            except Exception:  # noqa: BLE001 - an unreadable secret binds nothing; the runtime refuses it
                unreadable += len(refs)
                continue
            if target is None:
                unbound += len(refs)
                continue
            for ref in refs:
                if not secret_refs.is_ref(ref):
                    continue
                try:
                    if secret_store.rebind_provider_secret(conn, ref, target, db_path=db_path):
                        rebound += 1
                        services.add(service_id)
                except Exception:  # noqa: BLE001 - one unreadable secret does not stop the start; the runtime refuses it
                    unreadable += 1
    if unbound or unreadable or host_userinfo:
        print(f"[migration 13] URL credentials left unbound: {unbound} with no origin, {unreadable} unreadable; "
              f"services whose host holds user info that is not sent: {sorted(host_userinfo)}", flush=True)
    if not rebound:
        return {}
    from gateway import admin_store

    admin_store._record_audit_in_transaction(
        conn, AUDIT_ACTION,
        actor_kind="system", actor_id="migration", actor_label="Schema 13 migration",
        target_kind="operator_setting", target_id="service_registry",
        metadata={"services": sorted(services), "rebound": rebound, "unbound": unbound, "unreadable": unreadable},
        now=int(time.time()),
    )
    return {"rebound": rebound, "unbound": unbound, "unreadable": unreadable, "services": sorted(services)}

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

import copy

from gateway import extension_packages, record_carriers, record_integrity, secret_refs, secret_store

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


def redigest_packages(index: dict[str, Any], before: Any, after: Any, db_path: Path) -> int:
    """Replace every install proof of ``index`` with the keyed proof of the record as the registry now stores it.

    A package keeps a digest of each record it installed, to tell later whether an operator changed the record. Moving a
    record's secrets into the store changes the record, so every older digest would stop matching and a removal would
    detach every record instead of removing it. A record that still matched its old proof before this pass gets the keyed
    proof of its new form; one that did not (an operator had changed it) gets a proof that matches nothing, so it is
    still detached. The old bare digests are gone: a SHA-256 of a record that held a short secret can be guessed.
    Mutates ``index``; returns the number of packages re-digested (0 leaves the index and the key file untouched).
    """
    if not extension_packages.needs_keyed_digests(extension_packages.normalize_extension_package_index(index)):
        return 0
    normalized = extension_packages.normalize_extension_package_index(index)
    key = record_integrity.install_key(db_path)

    def records(registry: Any, kind: str) -> dict[str, Any]:
        section = registry.get(kind) if isinstance(registry, dict) else None
        return section if isinstance(section, dict) else {}

    count = 0
    for package_id, package in normalized["packages"].items():
        if "installed_records" not in package or package.get("installed_records_version") == extension_packages.INSTALLED_RECORDS_VERSION:
            continue
        states = package.get("disabled_states") if isinstance(package.get("disabled_states"), dict) else {}
        proofs: dict[str, Any] = {}
        for kind in sorted(extension_packages.DATA_OBJECT_KINDS):
            proofs[kind] = {}
            for record_id, entry in ((package["installed_records"] or {}).get(kind) or {}).items():
                state = (states.get(kind) or {}).get(record_id)
                old, new = records(before, kind).get(record_id), records(after, kind).get(record_id)
                matched = isinstance(old, dict) and isinstance(new, dict) and extension_packages._matches_install(
                    old, entry, package, state, key)
                proofs[kind][record_id] = {
                    "kind": kind, "id": record_id,
                    "mac": extension_packages.installed_record_digest(extension_packages.comparable_record(new, state), key) if matched else None,
                }
        package["installed_records"] = proofs
        package["installed_records_version"] = extension_packages.INSTALLED_RECORDS_VERSION
        index["packages"][package_id] = package
        count += 1
    return count


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
    registry_before = copy.deepcopy(registry)
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
        redigested = redigest_packages(new_index, registry_before, registry, db_path)
        if moved or redigested:
            _store(conn, PACKAGE_INDEX_KEY, new_index)
        if moved:
            changed["package_index"] = len(moved)
        if redigested:
            changed["package_proofs"] = redigested

    if changed:
        from gateway import admin_store

        admin_store._record_audit_in_transaction(
            conn, "operator.secrets.externalize", actor_kind="system", actor_id="migration",
            actor_label="Schema 11 migration", target_kind="operator_setting", target_id=REGISTRY_KEY,
            metadata=dict(changed), now=int(time.time()),
        )
    return changed

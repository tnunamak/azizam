"""The owner's permission for Azizam to run commands on this computer.

The setting is read from the live admin database the gateway bound at start,
never from the import-time default store. One migration keeps existing
installs working (decision D13): an install that already declares command,
process or systemd lifecycles gets the permission turned on once, with an
audit entry that names them. A new install stays off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from gateway import admin_store

SETTING = "allow_host_commands"
MIGRATION_MARKER = "host_commands_migration"
LEGACY_DRIVERS = frozenset({"command", "process", "systemd"})

_admin_db: Path | None = None


def bind_admin_db(path: Path | None) -> None:
    """Point the permission lookup at the gateway's live admin database."""
    global _admin_db
    _admin_db = Path(path) if path is not None else None


def commands_allowed() -> bool:
    try:
        setting = admin_store.get_operator_setting(SETTING, path=_admin_db or admin_store.DB_PATH)
        return bool(setting and setting.get("value", {}).get("enabled") is True)
    except Exception:  # setting store unavailable: host execution stays off
        return False


def _declared_drivers(service: Mapping[str, Any]) -> set[str]:
    """Every driver a record declares, in each place the registry keeps one."""
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    drivers: set[str] = set()
    for holder in (service, config):
        for key in ("lifecycle", "process"):
            block = holder.get(key)
            if isinstance(block, Mapping) and isinstance(block.get("driver"), str):
                drivers.add(block["driver"].strip().lower())
    return drivers & LEGACY_DRIVERS


def command_lifecycles(services: Mapping[str, Any]) -> list[dict[str, str]]:
    """The records that already rely on host commands, in a stable order."""
    found = []
    for service_id in sorted(services):
        record = services[service_id]
        if isinstance(record, Mapping):
            found.extend({"id": str(service_id), "driver": driver}
                         for driver in sorted(_declared_drivers(record)))
    return found


def migrate_legacy_command_lifecycles(path: Path, services: Mapping[str, Any]) -> list[dict[str, str]]:
    """Run the D13 migration once per database; return the lifecycles it enabled for."""
    with admin_store._conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if admin_store.get_operator_setting_in(conn, MIGRATION_MARKER) is not None:
                conn.rollback()
                return []
            found = command_lifecycles(services)
            enable = bool(found) and admin_store.get_operator_setting_in(conn, SETTING) is None
            if enable:
                admin_store.put_operator_setting_with_audit_in(
                    conn, SETTING, {"enabled": True}, updated_by="migration",
                    audit_action="operator.host_commands.migrate",
                    audit_actor_kind="system", audit_actor_id="migration",
                    audit_actor_label="Host command permission migration",
                    audit_target_kind="operator_setting", audit_target_id=SETTING,
                    audit_metadata={"enabled": True, "lifecycles": found},
                )
            admin_store.put_operator_setting_in(
                conn, MIGRATION_MARKER, {"done": True, "enabled": enable}, updated_by="migration")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return found if enable else []

"""One-time upgrade of stored transcription records to the D47 cut planner (admin schema version 14).

The rules are data (``registry/stt_chunking_upgrades.yml``): a record whose ``chunking`` block holds exactly the
listed old values gets the new keys. No record outside a rule changes.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

UPGRADES_PATH = Path(__file__).resolve().parent.parent / "registry" / "stt_chunking_upgrades.yml"


def load_upgrades(path: Path = UPGRADES_PATH) -> list[Mapping[str, Any]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [row for row in document.get("upgrades") or () if isinstance(row, Mapping)]


def upgraded_chunking(chunking: Any, upgrades: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The new ``chunking`` block, or None when no rule matches."""
    if not isinstance(chunking, Mapping):
        return None
    for rule in upgrades:
        when = rule.get("when") or {}
        if not when or any(chunking.get(key) != value for key, value in when.items()):
            continue
        result = copy.deepcopy(dict(chunking))
        for key in rule.get("drop") or ():
            result.pop(key, None)
        for key, value in (rule.get("set") or {}).items():
            result.setdefault(key, value)
        return result
    return None


def migrate_registry(conn: sqlite3.Connection) -> list[str]:
    """Upgrade matching records inside the caller's transaction; return their ids."""
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()
    if row is None:
        return []
    registry = json.loads(row[0])
    upgrades = load_upgrades()
    changed = []
    services = registry.get("services") if isinstance(registry, dict) else None
    for service_id, record in services.items() if isinstance(services, dict) else ():
        config = record.get("config") if isinstance(record, dict) else None
        if not isinstance(config, dict):
            continue
        new = upgraded_chunking(config.get("chunking"), upgrades)
        if new is not None and new != config["chunking"]:
            config["chunking"] = new
            changed.append(service_id)
    if changed:
        conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(registry),))
        conn.execute(
            "UPDATE operator_settings_generations SET operator_settings_generation=operator_settings_generation+1 "
            "WHERE setting_key='service_registry'"
        )
        from gateway import admin_store

        admin_store._record_audit_in_transaction(
            conn, "operator.stt_chunking.migrate", actor_kind="system", actor_id="migration",
            actor_label="Schema 14 migration", target_kind="operator_setting", target_id="service_registry",
            metadata={"services": changed}, now=int(time.time()),
        )
    return changed

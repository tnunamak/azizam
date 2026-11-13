"""Named step ``loaded-model-probe``: a service's ``state_type`` becomes the ``loaded_model_probe`` it meant.

The gateway used to pick the request that reads a service's loaded model from a table keyed by ``state_type``, and a
record without one was read the first way in the table. The probe is now data on the record. This step rewrites each
stored record once, from ``migration_data/loaded_model_probe.json``, so every record probes the same URL as before:

- a known ``state_type`` becomes its probe, or no field when it meant the default OpenAI-compatible model list;
- a record with no ``state_type`` whose loaded model is a path (``loaded_model_identity: path``) gets the probe that
  record was read through;
- an unknown ``state_type`` is dropped (the old code read nothing for it) and named in the audit entry.

A record that already declares ``loaded_model_probe`` keeps it. The step is recorded by name, so it runs once.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

STEP_NAME = "loaded-model-probe"
DATA_PATH = Path(__file__).resolve().parent / "migration_data" / "loaded_model_probe.json"


def _rewrite(config: dict[str, Any], data: dict[str, Any]) -> str | None:
    """Rewrite one record's config in place; return what happened, or None when nothing changed."""
    known = data["state_type"]
    if "state_type" in config:
        old = config.pop("state_type")
        if "loaded_model_probe" in config:
            return "kept-declared-probe"
        if isinstance(old, str) and old in known:
            if known[old] is not None:
                config["loaded_model_probe"] = dict(known[old])
                return "probe"
            return "default"
        return "unknown-state-type"
    if config.get("loaded_model_identity") == "path" and "loaded_model_probe" not in config:
        config["loaded_model_probe"] = dict(data["path_identity"])
        return "probe"
    return None


def migrate_registry(conn: sqlite3.Connection) -> dict[str, str]:
    """Rewrite the stored service registry in the caller's transaction; return {service_id: outcome}."""
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()
    if row is None:
        return {}
    registry = json.loads(row[0])
    services = registry.get("services") if isinstance(registry, dict) else None
    if not isinstance(services, dict):
        return {}
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    changed: dict[str, str] = {}
    for service_id, record in services.items():
        config = record.get("config") if isinstance(record, dict) else None
        if isinstance(config, dict):
            outcome = _rewrite(config, data)
            if outcome is not None:
                changed[str(service_id)] = outcome
    if not changed:
        return {}
    conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(registry),))
    conn.execute(
        "UPDATE operator_settings_generations SET operator_settings_generation=operator_settings_generation+1 "
        "WHERE setting_key='service_registry'"
    )
    from gateway import admin_store

    admin_store._record_audit_in_transaction(
        conn, "operator.loaded_model_probe.migrate", actor_kind="system", actor_id="migration",
        actor_label="Loaded-model probe migration", target_kind="operator_setting", target_id="service_registry",
        metadata={"services": changed}, now=int(time.time()),
    )
    return changed

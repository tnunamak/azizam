"""One-time D-1 compatibility data migration (admin schema version 5).

No runtime dispatch policy belongs here. New services opt in explicitly;
removing an attachment after upgrade must survive every subsequent startup.
"""
from __future__ import annotations

import json
import sqlite3

from gateway import service_registry


def migrate_registry(conn: sqlite3.Connection) -> list[str]:
    """Attach the former automatic translation, inside the caller's transaction.

    Match enabled native chat/completion services. Remote providers and the
    execution substrate never received the local translation. Disabled rows
    remain unchanged; their eventual activation is a new operator choice.
    """
    row = conn.execute(
        "SELECT value_json FROM operator_settings WHERE key='service_registry'"
    ).fetchone()
    if row is None:
        return []
    registry = json.loads(row[0])
    changed = []
    for service_id, service in registry.get("services", {}).items():
        runtime = service_registry.runtime_backend_from_service(service)
        capabilities = service_registry.normalize_string_list(runtime.get("capabilities"))
        if (
            not service_registry.bool_from_config(runtime.get("enabled"), True)
            or not {str(cap).strip().lower() for cap in capabilities} & {"chat", "completions"}
            or not runtime.get("port")
            or service_registry.is_remote_provider_config(runtime)
            or service_registry.is_litellm_service_record(service_id, service)
        ):
            continue
        config = service.setdefault("config", {})
        transforms = config.get("transforms")
        # The old pipeline ignored non-list attachments entirely.
        transforms = list(transforms) if isinstance(transforms, list) else []
        if "reasoning-effort" in transforms:
            continue
        config["transforms"] = [*transforms, "reasoning-effort"]
        changed.append(service_id)
    if changed:
        conn.execute(
            "UPDATE operator_settings SET value_json=? WHERE key='service_registry'",
            (json.dumps(registry),),
        )
        conn.execute(
            "UPDATE operator_settings_generations "
            "SET operator_settings_generation=operator_settings_generation+1 "
            "WHERE setting_key='service_registry'"
        )
    return changed

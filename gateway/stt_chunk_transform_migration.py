"""One-time P2-6 compatibility data migration (admin schema version 6).

Before version 6 the gateway dropped a chunk response's ``timing`` field for
every STT service, because one engine family adds that per-chunk diagnostic.
The rule is now a response transform row on the service. Attach it once to each
service that offers transcriptions, so every existing service keeps merging
chunked transcripts as before; a later removal survives every startup.
"""
from __future__ import annotations

import copy
import json
import sqlite3
from typing import Any, Mapping

from gateway import service_registry
from gateway.compatibility import COMPATIBILITY

TIMING_DIAGNOSTIC_ROW: dict[str, Any] = {"kind": "drop-params", "phase": "response", "drop": ["timing"]}


def attach_timing_row(config: dict[str, Any], capabilities: Any) -> bool:
    """Append the row to a speech-to-text service's config unless it is there; True when added."""
    declared = {str(cap).strip().lower() for cap in service_registry.normalize_string_list(capabilities)}
    selector = COMPATIBILITY.migration
    if declared.intersection(selector.exclude_capabilities) or not (
            declared.intersection(selector.include_capabilities) or isinstance(config.get(selector.config_mapping), dict)):
        return False
    transforms = config.get("transforms")
    # The old chunk parser never read attachments, so a non-list value carried no rows.
    transforms = list(transforms) if isinstance(transforms, list) else []
    if TIMING_DIAGNOSTIC_ROW in transforms:
        return False
    config["transforms"] = [*transforms, copy.deepcopy(TIMING_DIAGNOSTIC_ROW)]
    return True


def migrated_backends(backends: Mapping[str, Any]) -> dict[str, Any]:
    """Runtime backend configs after the migration (builds the differential fixture)."""
    out = copy.deepcopy(dict(backends))
    for config in out.values():
        attach_timing_row(config, config.get("capabilities"))
    return out


def migrate_registry(conn: sqlite3.Connection) -> list[str]:
    """Attach the row inside the caller's transaction, enabled or not: the old rule applied to all."""
    row = conn.execute(
        "SELECT value_json FROM operator_settings WHERE key='service_registry'"
    ).fetchone()
    if row is None:
        return []
    registry = json.loads(row[0])
    changed = []
    for service_id, service in registry.get("services", {}).items():
        capabilities = service_registry.runtime_backend_from_service(service).get("capabilities")
        config = service.get("config")
        if isinstance(config, dict) and attach_timing_row(config, capabilities):
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

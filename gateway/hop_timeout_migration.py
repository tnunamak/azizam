"""Schema 11: discard the old engine page's implicit 600 second hop limit."""
from __future__ import annotations

import json
import sqlite3
import time

from gateway import litellm_substrate, service_registry


def migrate_registry(conn: sqlite3.Connection) -> list[str]:
    """Remove only numeric 600 on hop-routed records, in the upgrade transaction."""
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()
    if row is None:
        return []
    registry = json.loads(row[0])
    changed = []
    services = registry.get('services') if isinstance(registry, dict) else None
    for service_id, record in services.items() if isinstance(services, dict) else ():
        if not isinstance(record, dict) or not isinstance(record.get('config'), dict):
            continue
        config = record['config']
        if type(config.get('timeout')) not in (int, float) or config['timeout'] != 600:
            continue
        try:
            service = service_registry.normalize_service_record(service_id, record)
        except (ValueError, TypeError, KeyError):
            continue  # a refused record stays byte-for-byte as stored for repair
        if not litellm_substrate._is_openai_compatible(service):
            continue
        if not config.get('gateway_lifecycle'):
            deployments = litellm_substrate._deployment_records(service)
            if not any(deployment['gateway_lifecycle'] for deployment in deployments):
                continue
        del config['timeout']
        changed.append(service_id)
    if not changed:
        return []
    conn.execute("UPDATE operator_settings SET value_json=? WHERE key='service_registry'", (json.dumps(registry),))
    conn.execute(
        "UPDATE operator_settings_generations SET operator_settings_generation=operator_settings_generation+1 "
        "WHERE setting_key='service_registry'"
    )
    from gateway import admin_store

    admin_store._record_audit_in_transaction(
        conn, 'operator.hop_timeout.migrate', actor_kind='system', actor_id='migration',
        actor_label='Schema 11 migration', target_kind='operator_setting', target_id='service_registry',
        metadata={'services': changed}, now=int(time.time()),
    )
    return changed

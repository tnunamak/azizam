#!/usr/bin/env python3
"""Move engine-named code tables into service-registry data, in place.

The gateway used to carry a table of backend ids for each behaviour that
differs by engine (absolute-path activation, aux model probe, loaded-model
identity, vision from an aux model, context window from a live probe). Each
behaviour is now a plain field on the service record. This script writes those
fields onto the operator's existing service and removes the Tabby service row
(the product no longer supports Tabby).

Dry-run is the default. Apply only after stopping Azizam, or explicitly use
--allow-running (a restart is still required to reload the registry). There is
no compatibility path: after this migration the gateway reads only the fields.
Run it once per database; every step is idempotent.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retire_legacy_names import read_connection, require_stopped, snapshot_signature  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SETTING = "service_registry"
UPDATED_BY = "migration:engine-service-fields"


def _services(registry: dict) -> dict:
    services = registry.get("services")
    return services if isinstance(services, dict) else {}


# The owner's current local chat service. The fields below are written to this
# service record only; the gateway reads them from whichever service declares them.
OWNER_LOCAL_SERVICE_ID = "kobold"


def _set_driver_config(service: dict, key: str, value) -> bool:
    """Set a lifecycle driver_config field on both copies of the service's lifecycle block."""
    changed = False
    holders = [service.get("lifecycle")]
    config = service.get("config")
    if isinstance(config, dict):
        holders.append(config.get("lifecycle"))
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        driver_config = holder.setdefault("driver_config", {})
        if driver_config.get(key) != value:
            driver_config[key] = value
            changed = True
    return changed


def _set_config(service: dict, key: str, value) -> bool:
    config = service.setdefault("config", {})
    if config.get(key) == value:
        return False
    config[key] = value
    return True


def seed_loaded_model_identity(registry: dict) -> list[str]:
    service = _services(registry).get(OWNER_LOCAL_SERVICE_ID)
    if service is None or not _set_config(service, "loaded_model_identity", "path"):
        return []
    return [f"{OWNER_LOCAL_SERVICE_ID}: config.loaded_model_identity = path"]


def seed_activation_absolute_path(registry: dict) -> list[str]:
    service = _services(registry).get(OWNER_LOCAL_SERVICE_ID)
    if service is None or not _set_driver_config(service, "activation_requires_absolute_path", True):
        return []
    return [f"{OWNER_LOCAL_SERVICE_ID}: lifecycle.driver_config.activation_requires_absolute_path = true"]


def remove_disabled_tabby(registry: dict) -> list[str]:
    """Delete the disabled `tabby` service row. An enabled row is never touched."""
    services = _services(registry)
    row = services.get("tabby")
    if row is None:
        return []
    if row.get("enabled") is not False:
        raise ValueError("service 'tabby' is enabled; disable it before migrating")
    del services["tabby"]
    return ["remove disabled service row 'tabby'"]


# Each step: (name, function). A function edits the registry in place and
# returns human-readable change lines (empty when there is nothing to do).
STEPS: list[tuple[str, Callable[[dict], list[str]]]] = [
    ("remove-disabled-tabby", remove_disabled_tabby),
    ("activation-absolute-path", seed_activation_absolute_path),
    ("loaded-model-identity", seed_loaded_model_identity),
]


def migrate_registry(registry: dict) -> list[str]:
    changes: list[str] = []
    for _name, step in STEPS:
        changes.extend(step(registry))
    return changes


def load_registry(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (SETTING,)).fetchone()
    if row is None:
        return None
    registry = json.loads(row[0])
    if not isinstance(registry, dict):
        raise ValueError("service registry is not a JSON object")
    return registry


def service_ids(registry: dict) -> list[str]:
    return sorted(_services(registry))


def apply_changes(path: Path, *, allow_running: bool) -> tuple[list[str], Path | None]:
    if not allow_running:
        require_stopped()
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            registry = load_registry(conn)
            if registry is None:
                conn.rollback()
                return [], None
            changes = migrate_registry(registry)
            if not changes:
                conn.rollback()
                return [], None
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup = path.with_name(path.name + ".bak.engine-service-fields." + stamp)
            with backup.open("xb"):
                backup.chmod(0o600)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
            if not allow_running:
                require_stopped()
            conn.execute(
                "UPDATE operator_settings SET value_json=?, updated_at=?, updated_by=? WHERE key=?",
                (json.dumps(registry, sort_keys=True, separators=(",", ":")), int(time.time()), UPDATED_BY, SETTING),
            )
            conn.execute(
                "INSERT INTO operator_settings_generations(setting_key, operator_settings_generation) "
                "VALUES (?, 1) ON CONFLICT(setting_key) DO UPDATE SET "
                "operator_settings_generation=operator_settings_generation+1",
                (SETTING,),
            )
            conn.commit()
            return changes, backup
        except BaseException:
            conn.rollback()
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/admin.db")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="inspect without writing (default)")
    mode.add_argument("--apply", action="store_true", help="back up and migrate the registry")
    parser.add_argument("--allow-running", action="store_true", help="permit inspection/migration while gateway runs")
    args = parser.parse_args(argv)
    path = args.db.expanduser().resolve()
    try:
        if args.apply:
            changes, backup = apply_changes(path, allow_running=args.allow_running)
        else:
            if not args.allow_running:
                require_stopped()
            before = snapshot_signature(path)
            with closing(read_connection(path)) as conn:
                registry = load_registry(conn)
            changes = migrate_registry(registry) if registry is not None else []
            if before != snapshot_signature(path):
                raise RuntimeError("database changed during inspection; retry for a consistent dry-run")
            backup = None
        print("APPLY" if args.apply else "DRY RUN (no writes)")
        for change in changes:
            print(f"  service_registry: {change}")
        print(f"{len(changes)} {'change' if len(changes) == 1 else 'changes'} {'applied' if args.apply else 'to apply'}")
        if backup:
            print(f"Backup: {backup}")
        if args.allow_running:
            print("Running-gateway check bypassed explicitly; restart after apply to reload state.")
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"Migration refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

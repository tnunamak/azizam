"""Copy `model_metadata` from proxy_config.yml model records into the DB-owned service registry.

After first boot, admin.db owns each service's `models` list and YAML edits to
`backends.<id>.models` never reach it (see register_service_from_config.py). This script
changes one key only: for every YAML model record that declares `model_metadata`, it sets
that key on the registry record with the same `id`. Other fields, records and services stay
as they are. It writes through the audited compare-and-swap registry writer, after it backs
up the database. It writes no other file: it does not regenerate LiteLLM's config and does
not trigger any reload. Running it twice changes nothing the second time.

    ./venv/bin/python scripts/local/sync_model_metadata_from_config.py            # dry run
    ./venv/bin/python scripts/local/sync_model_metadata_from_config.py --apply

Options: --db PATH (default data/admin.db), --config PATH (default proxy_config.yml),
--service ID (limit to one service). Run it from the repository root. The gateway reads the
change on its next start; this script never restarts anything. model_metadata is not part of
LiteLLM's compiled config, so that restart leaves LiteLLM's config file unchanged.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway import admin_store  # noqa: E402
from gateway import private_io  # noqa: E402
from gateway.service_registry import SERVICE_REGISTRY_SETTING_KEY  # noqa: E402
from gateway.model_metadata import DECLARATION_KEY  # noqa: E402


def _backup(db: Path) -> Path:
    """Copy the database to a new file that is never reused, readable only by the owner."""
    target = db.with_name(f"{db.name}.bak.pre-model-metadata-{time.time_ns()}")
    private_io.create_private_file(target)
    with sqlite3.connect(db) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    return target


def plan_changes(
    backends: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    service: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Return (updated registry, changes, warnings). Pure: never touches the input."""
    updated = copy.deepcopy(dict(registry))
    services = updated.get("services") or {}
    changes: list[dict[str, Any]] = []
    warnings: list[str] = []
    for service_id, raw in backends.items():
        if service and service_id != service:
            continue
        yaml_models = raw.get("models") if isinstance(raw, Mapping) else None
        if not isinstance(yaml_models, list):
            continue
        declared = {
            str(m.get("id")): m[DECLARATION_KEY]
            for m in yaml_models
            if isinstance(m, Mapping) and m.get("id") and isinstance(m.get(DECLARATION_KEY), Mapping)
        }
        if not declared:
            continue
        record = services.get(service_id)
        db_models = (record or {}).get("config", {}).get("models") if isinstance(record, Mapping) else None
        if not isinstance(db_models, list):
            warnings.append(f"service {service_id!r} has no models list in the registry; skipped")
            continue
        by_id = {str(m.get("id")): m for m in db_models if isinstance(m, dict) and m.get("id")}
        for model_id, metadata in declared.items():
            target = by_id.get(model_id)
            if target is None:
                warnings.append(f"{service_id}/{model_id}: no registry record with this id; skipped")
                continue
            if target.get(DECLARATION_KEY) == metadata:
                continue
            changes.append({
                "service": service_id,
                "model": model_id,
                "before": copy.deepcopy(target.get(DECLARATION_KEY)),
                "after": copy.deepcopy(dict(metadata)),
            })
            target[DECLARATION_KEY] = copy.deepcopy(dict(metadata))
    return updated, changes, warnings


def write_model_metadata(
    db: Path,
    updated: Mapping[str, Any],
    changes: list[dict[str, Any]],
    *,
    expected_generation: int,
) -> dict[str, Any]:
    """Commit the registry, its generation and one audit row, and nothing else.

    This calls the audited compare-and-swap writer directly. The admin API's
    wrapper would also rewrite LiteLLM's config file (wherever the registry's
    `config_path` points) and fire the hot reload, which a key-only edit to a
    possibly copied database must never do.
    """
    return admin_store.write_service_registry_with_audit(
        updated,
        expected_generation=expected_generation,
        updated_by="sync-model-metadata-from-config",
        audit_action="operator.service_registry.update",
        audit_actor_kind="system",
        audit_actor_id="sync-model-metadata-from-config",
        audit_actor_label="sync model_metadata",
        audit_target_kind="operator_setting",
        audit_target_id=SERVICE_REGISTRY_SETTING_KEY,
        audit_metadata={
            "field": DECLARATION_KEY,
            "models": sorted(f"{c['service']}/{c['model']}" for c in changes),
        },
        path=db,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("data/admin.db"))
    parser.add_argument("--config", type=Path, default=Path("proxy_config.yml"))
    parser.add_argument("--service", default=None, help="only this backends.<id>")
    parser.add_argument("--apply", action="store_true", help="write the change (default: print the plan only)")
    args = parser.parse_args(argv)

    backends = (yaml.safe_load(args.config.read_text()) or {}).get("backends") or {}
    current = admin_store.get_service_registry_with_generation(path=args.db)
    if current is None:
        print(f"error: {args.db} has no service registry yet; start the gateway once to seed it", file=sys.stderr)
        return 2
    generation = current["operator_settings_generation"]
    updated, changes, warnings = plan_changes(backends, current["value"], service=args.service)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(json.dumps(changes, indent=2, sort_keys=True))
    if not changes:
        print("\nnothing to change", file=sys.stderr)
        return 0
    if not args.apply:
        print(f"\ndry run: {len(changes)} record(s), registry generation {generation}; pass --apply to write", file=sys.stderr)
        return 0

    backup = _backup(args.db)
    record = write_model_metadata(args.db, updated, changes, expected_generation=generation)
    print(f"\napplied: generation {generation} -> {record['operator_settings_generation']}; backup {backup}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

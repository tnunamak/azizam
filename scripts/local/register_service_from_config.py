"""Add one `backends.<id>` block of proxy_config.yml to the DB-owned service registry.

After first boot the registry in admin.db is the source of truth for services; edits to the
`backends:` section of proxy_config.yml only seed a fresh instance. This script copies one block
into a populated registry with the same call the admin API uses (compare-and-swap on the registry
generation), after it backs up the database.

    ./venv/bin/python scripts/local/register_service_from_config.py rerank            # dry run
    ./venv/bin/python scripts/local/register_service_from_config.py rerank --apply

Options: --db PATH (default data/admin.db), --config PATH (default proxy_config.yml),
--replace (overwrite an existing id). Run it from the repository root. The gateway picks the
change up on its next start (or hot reload of services); this script never restarts anything.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway import admin_store, service_registry  # noqa: E402
from gateway.admin_api import AdminContext  # noqa: E402
from gateway.admin_runtime import AdminSQLiteStore  # noqa: E402


def _backup(db: Path) -> Path:
    target = db.with_name(f"{db.name}.bak.pre-register-{int(time.time())}")
    admin_store.secure_state_path(db)
    admin_store.secure_state_path(target)
    with sqlite3.connect(db) as source, sqlite3.connect(target) as dest:
        source.backup(dest)  # consistent copy even while the gateway has the file open
    admin_store.secure_state_path(target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("service_id")
    parser.add_argument("--db", type=Path, default=Path("data/admin.db"))
    parser.add_argument("--config", type=Path, default=Path("proxy_config.yml"))
    parser.add_argument("--apply", action="store_true", help="write the change (default: print the record only)")
    parser.add_argument("--replace", action="store_true", help="overwrite the record if the id exists")
    args = parser.parse_args(argv)

    backends = (yaml.safe_load(args.config.read_text()) or {}).get("backends") or {}
    raw = backends.get(args.service_id)
    if not isinstance(raw, dict):
        print(f"error: {args.config} has no backends.{args.service_id}", file=sys.stderr)
        return 2

    current = admin_store.get_service_registry_with_generation(path=args.db)
    if current is None:
        print(f"error: {args.db} has no service registry yet; start the gateway once to seed it", file=sys.stderr)
        return 2
    generation = current["operator_settings_generation"]
    exists = args.service_id in (current["value"].get("services") or {})
    if exists and not args.replace:
        print(f"error: service {args.service_id!r} already exists; pass --replace to overwrite", file=sys.stderr)
        return 3
    updated, record = service_registry.upsert_service_in_registry(
        current["value"], service_id=args.service_id, raw=raw, source="ui", replace=exists
    )
    print(json.dumps(record, indent=2, sort_keys=True))
    if not args.apply:
        print(f"\ndry run: registry generation {generation}; pass --apply to write", file=sys.stderr)
        return 0

    backup = _backup(args.db)
    actor = AdminContext(
        subject="register-service-from-config",
        actor_kind="system",
        actor_label=f"register {args.service_id}",
        roles=frozenset({"admin"}),
    )
    result = AdminSQLiteStore(args.db).update_service_registry(
        data=updated, actor=actor, expected_generation=generation
    )
    print(f"\napplied: generation {generation} -> {result.get('operator_settings_generation')}; backup {backup}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

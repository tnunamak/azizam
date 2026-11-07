#!/usr/bin/env python3
"""Dry run of the one-time step ``workflow-schema-2`` (docs/design/CODE_TO_DATA_S3.md, D3). Reads a copy; writes nothing.

    python scripts/workflow_schema_2_report.py COPY_OF_ADMIN_DB [--json]

Opens COPY_OF_ADMIN_DB read-only and plans what a start would do with each stored workflow record: convert a whole record,
convert a patch over a release record, leave a schema 2 record alone, or mark a record that cannot convert as needing
configuration (stored disabled, with its errors, never dropped). A registry stored before the layering step is layered in
memory first, as a start does. It prints record ids, field names, error pointers and error codes only, never a value of any
record and never an error message.

Exit status: 0 when the step would run (or has run) and every record converts; 1 when some record cannot convert; 2 for a
document that cannot be read.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import registry_layers, registry_migration, service_registry, workflow_schema_migration  # noqa: E402
from gateway.discovery import bundled_workflow_manifests  # noqa: E402
from scripts.registry_layering_report import read_stored  # noqa: E402


def step_recorded(path: Path) -> bool:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return connection.execute(
            "SELECT 1 FROM schema_migration_steps WHERE name = ?", (workflow_schema_migration.STEP,)).fetchone() is not None
    except sqlite3.OperationalError:  # a database that predates the step table has not run it
        return False
    finally:
        connection.close()


def build_report(path: Path) -> tuple[dict, int]:
    stored = read_stored(path)
    if stored is None:
        return {"state": "no registry stored: a start writes the empty owner document, which holds no workflow record"}, 0
    if step_recorded(path):
        return {"state": "already done: the step has nothing to do"}, 0
    release = service_registry.release_registry()
    if not registry_layers.is_layered(stored):  # a start layers first
        stored = registry_migration.plan_layering(
            stored, release, service_registry.load_seed_catalogs({}),
            owner_edited_services=registry_migration.owner_edited_services(path)).stored
    plan = workflow_schema_migration.plan_workflow_schema_2(
        stored, release, bundled_ids=frozenset(bundled_workflow_manifests()))
    report = plan.report()
    state = "would convert" if plan.changes else "nothing to convert"
    return {"state": state, **report}, 1 if report["needs_configuration"] else 0


def render(report: dict) -> str:
    lines = [f"state: {report['state']}"]
    if "counts" in report:
        lines.append("counts: " + ", ".join(f"{name} {count}" for name, count in report["counts"].items()))
    for row in report.get("records", []):
        notes = []
        if row["overrides_bundled"]:
            notes.append("overrides a bundled workflow")
        if row["package"]:
            notes.append(f"package {row['package']}")
        lines.append(f"{row['action']:<14}{row['id']}  fields: {', '.join(row['fields']) or '-'}"
                     + (f"  ({'; '.join(notes)})" if notes else ""))
    for row in report.get("needs_configuration", []):
        errors = ", ".join(f"{item['pointer']} {item['code']}" for item in row["errors"]) or "-"
        lines.append(f"needs configuration  {row['id']}  errors: {errors}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db", type=Path, help="a COPY of admin.db; opened read-only")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    try:
        report, status = build_report(args.db)
    except sqlite3.Error as error:
        print(f"cannot read {args.db}: {type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    return status


if __name__ == "__main__":
    raise SystemExit(main())

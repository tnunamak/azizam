#!/usr/bin/env python3
"""Dry run of the one-time registry layering step (docs/design/CONFIG_HOMES.md, 8.3). Reads a copy; writes nothing.

    python scripts/registry_layering_report.py COPY_OF_ADMIN_DB [--json] [--gate]

Opens COPY_OF_ADMIN_DB read-only, plans the step ``config-homes-registry-layering`` and prints, for each stored record
that has a release record of the same id, the NAMES of the fields it would keep as a patch and whether the audit log
shows an owner edit of it (a patch with none is probably a stale copy of an older release). It prints record ids and
field names only, never a value of any record.

``--gate`` also compares the registry the old boot served with the one a load serves after the step, record by record,
and lists the ids that differ (none is the pass). The route table, the frontend mount table, /v1/models and the alias
table follow from that registry; ``tests/test_registry_layering_gate.py`` compares them on a fixture.

Exit status: 0 when the step would run (or has run) and, with ``--gate``, nothing differs; 1 when the gate finds a
difference; 2 for a document that cannot be read.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import registry_layers, registry_migration, service_registry  # noqa: E402


def read_stored(path: Path) -> dict | None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT value_json FROM operator_settings WHERE key = 'service_registry'").fetchone()
    finally:
        connection.close()
    return None if row is None else service_registry._stored_document(row[0])


def build_report(path: Path, *, gate: bool) -> tuple[dict, int]:
    stored = read_stored(path)
    if stored is None:
        return {"state": "no registry stored: a start writes the empty owner document, which inherits every release record"}, 0
    if registry_layers.is_layered(stored):
        return {"state": "already layered: the step has nothing to do"}, 0
    release = service_registry.release_registry()
    catalogs = service_registry.load_seed_catalogs({})
    plan = registry_migration.plan_layering(
        stored, release, catalogs, owner_edited_services=registry_migration.owner_edited_services(path))
    report = {"state": "would layer", **plan.report()}
    status = 0
    if gate:
        before, _carried = registry_migration.effective_before(stored, catalogs)
        report["gate_differing_records"] = registry_migration.differing_records(
            before, registry_migration.effective_after(plan, release))
        status = 1 if report["gate_differing_records"] else 0
    return report, status


def render(report: dict) -> str:
    lines = [f"state: {report['state']}"]
    for row in report.get("patches", []):
        edit = {True: "yes", False: "no (probably a stale copy)", None: "unknown (no audit rows for this kind)"}[row["owner_edit_in_audit_log"]]
        lines.append(f"patch   {row['collection']}/{row['id']}  {row['kind']}  fields: {', '.join(row['fields']) or '-'}  owner edit in audit log: {edit}")
    for label, key in (("inherit", "inherit"), ("owner-added", "owner_added"), ("tombstone", "tombstones")):
        for collection, ids in (report.get(key) or {}).items():
            lines.append(f"{label:<8}{collection}: {', '.join(ids)}")
    if report.get("carried_excluded"):
        lines.append(f"carried as stored (do not validate): {', '.join(report['carried_excluded'])}")
    if "gate_differing_records" in report:
        differing = report["gate_differing_records"]
        lines.append("gate: equal before and after" if not differing else f"gate: DIFFERS for {', '.join(differing)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db", type=Path, help="a COPY of admin.db; opened read-only")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--gate", action="store_true", help="compare the registry served before and after, by record id")
    args = parser.parse_args(argv)
    try:
        report, status = build_report(args.db, gate=args.gate)
    except sqlite3.Error as error:
        print(f"cannot read {args.db}: {type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    return status


if __name__ == "__main__":
    raise SystemExit(main())

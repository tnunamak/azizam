#!/usr/bin/env python3
"""Pre-deploy count for C5 (docs/design/CONFIG_HOMES.md, 8.4): stored workflows that have the id of a bundled one. Reads a copy.

    python scripts/workflow_override_report.py COPY_OF_ADMIN_DB [--json]

C5 makes a stored manifest win over the bundled manifest with its id. Before C5 the bundled one won and the stored copy was
skipped, so each stored workflow listed here changes what the gateway serves once C5 is live. The expected count on a live
install is zero (the flip then changes nothing). Opens COPY_OF_ADMIN_DB read-only, writes nothing, and prints workflow ids and
the NAMES of the manifest fields that differ from the bundled manifest, never a value.

Exit status: 0 when no stored workflow has a bundled id; 1 when some do (the report lists them); 2 for a database that cannot be read.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import service_registry  # noqa: E402
from scripts.registry_layering_report import read_stored  # noqa: E402


def build_report(path: Path) -> dict:
    stored = read_stored(path)
    workflows = (stored or {}).get("workflows") if isinstance(stored, dict) else None
    registry = {"workflows": {
        record_id: {"manifest": (record.get("manifest") if isinstance(record, dict) and isinstance(record.get("manifest"), dict) else record)}
        for record_id, record in (workflows or {}).items() if isinstance(record, dict)
    }}
    overrides = service_registry.workflow_overrides(registry)
    return {"stored_workflows": len(registry["workflows"]), "with_bundled_id": overrides}


def render(report: dict) -> str:
    lines = [f"stored workflows: {report['stored_workflows']}", f"with a bundled id: {len(report['with_bundled_id'])}"]
    for workflow_id, row in sorted(report["with_bundled_id"].items()):
        lines.append(f"override {workflow_id}  fields that differ: {', '.join(row['fields']) or '-'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db", type=Path, help="a COPY of admin.db; opened read-only")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.db)
    except sqlite3.Error as error:
        print(f"cannot read {args.db}: {type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    return 1 if report["with_bundled_id"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

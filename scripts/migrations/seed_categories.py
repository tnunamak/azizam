#!/usr/bin/env python3
"""Create the starter categories and file workflows or models into them.

A category is a named group of models and workflows with one rule: who may see
what is in it. This script writes the same rows the Categories page writes, so
the owner can edit or delete every one of them there afterwards:

* "Adult": hidden from everyone except the owner, until the owner names the
  access plans that may use it.
* "Experimental": the same, for work that is still being built. The owner moves
  an item out of Experimental to promote it.

Categories that already exist are left exactly as they are, so a second run
changes nothing and never undoes an edit the owner made on the page. An item
that is already in the category is skipped.

Which workflow or model goes where is a command-line argument, never code:

    seed_categories.py --put experimental=image.generate.some-workflow

Dry-run is the default and prints what would change. Add --apply to write. The
gateway need not stop: the change is one compare-and-swap activation that the
running gateway picks up on its next request.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gateway import category_policy as policy  # noqa: E402
from gateway.category_api import snapshot_from_json, snapshot_to_json  # noqa: E402
from gateway.category_store import CategoryStore, NoActiveSnapshotError  # noqa: E402

AUTHOR_FALLBACK = "migration:seed-categories"
MEMBER_VALUE = "included"  # The one value of a starter category: "this item is in it".
SOURCE = "owner"
MAPPING_VERSION = "owner-edit-1"

# Category rows, as the Categories page would write them. Data, not behaviour:
# the gateway reads ``visible_to`` and never looks at these ids or labels.
STARTER_CATEGORIES: tuple[dict[str, Any], ...] = (
    {
        "id": "adult",
        "label": "Adult",
        "description": "Content for adults only.",
    },
    {
        "id": "experimental",
        "label": "Experimental",
        "description": "Still being built, so it may not work yet.",
    },
)


def _empty_snapshot() -> dict[str, Any]:
    return {"id": "owner-rules", "categories": {}, "people": {}, "apps": {},
            "services": {}, "completeness_gate": True, "retained": {}}


def _category_row(seed: dict[str, Any]) -> dict[str, Any]:
    return {"id": seed["id"], "label": seed["label"], "kind": "set",
            "values": [MEMBER_VALUE], "applies_to": ["release", "workflow"],
            "description": seed["description"], "visible_to": []}


def _service_for(snapshot: dict[str, Any], stem: str) -> tuple[str, dict[str, Any]]:
    for key, service in snapshot["services"].items():
        if key.rsplit("@", 1)[0] == stem:
            return key, service
    identifier = stem + "@r1"
    service = {"id": identifier, "dependencies": [], "checkable": True,
               "members": [{"id": identifier, "kind": "workflow", "assertions": []}]}
    snapshot["services"][identifier] = service
    return identifier, service


def plan_changes(snapshot: dict[str, Any], puts: list[tuple[str, str]], owner: str,
                 now: str) -> list[str]:
    """Edit ``snapshot`` in place and return one plain line per change."""
    lines: list[str] = []
    for seed in STARTER_CATEGORIES:
        if seed["id"] not in snapshot["categories"]:
            snapshot["categories"][seed["id"]] = _category_row(seed)
            lines.append(f"add category {seed['label']}: visible to the owner only")
    for category, stem in puts:
        if category not in snapshot["categories"]:
            raise SystemExit(f"error: no category {category!r}; known: {sorted(snapshot['categories'])}")
        _, service = _service_for(snapshot, stem)
        root = service["members"][0]
        if any(a["category"] == category and a["accepted"] for a in root["assertions"]):
            continue
        root["assertions"].append({
            "category": category, "value": MEMBER_VALUE, "source": SOURCE, "accepted": True,
            "observed_at": now, "accepted_at": now, "accepting_owner": owner,
            "mapping_version": MAPPING_VERSION,
        })
        lines.append(f"put {stem} in {snapshot['categories'][category]['label']}")
    return lines


def _owner_id(admin_db: Path) -> str | None:
    if not admin_db.exists():
        return None
    with sqlite3.connect(f"file:{admin_db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT r.subject_id FROM admin_role_assignments r JOIN users u ON u.id = r.subject_id "
            "WHERE r.role = 'owner' AND r.subject_kind = 'user' AND r.revoked_at IS NULL "
            "ORDER BY r.granted_at, r.id LIMIT 1").fetchone()
    return str(row[0]) if row else None


def _known_workflows(admin_db: Path) -> set[str]:
    from gateway import service_registry
    from gateway.discovery import WorkflowRegistry
    registry = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")
    admin = service_registry.load_service_registry(path=admin_db) if admin_db.exists() else None
    if admin is not None:
        for record in service_registry.workflow_records_from_registry(admin):
            if registry.get(record.id) is None:
                registry.add_manifest(record.manifest, source_path=record.source_path)
    return {record.id for record in registry.records()}


def _parse_put(text: str) -> tuple[str, str]:
    category, separator, stem = text.partition("=")
    if not separator or not category or not stem:
        raise argparse.ArgumentTypeError("use CATEGORY=ID, for example experimental=image.generate.some-workflow")
    return category, stem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data",
                        help="directory holding categories.db and admin.db (default: ./data)")
    parser.add_argument("--put", action="append", default=[], type=_parse_put, metavar="CATEGORY=ID",
                        help="file a workflow into a category (repeatable)")
    parser.add_argument("--unchecked", action="store_true",
                        help="skip the check that each --put id is a known workflow")
    parser.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    args = parser.parse_args(argv)

    data_dir: Path = args.data_dir
    admin_db = data_dir / "admin.db"
    owner = _owner_id(admin_db)
    if owner is None:
        print(f"error: no owner found in {admin_db}", file=sys.stderr)
        return 2
    if not args.unchecked and args.put:
        known = _known_workflows(admin_db)
        unknown = sorted({stem for _, stem in args.put} - known)
        if unknown:
            print(f"error: not workflows in this catalog: {unknown} (use --unchecked for models)", file=sys.stderr)
            return 2

    store_path = data_dir / "categories.db"
    store = CategoryStore(store_path) if args.apply or store_path.exists() else None
    try:
        active = store.get_active() if store else None
    except NoActiveSnapshotError:
        active = None
    snapshot = json.loads(json.dumps(active.body)) if active else _empty_snapshot()
    now = datetime.now(timezone.utc).isoformat()
    lines = plan_changes(snapshot, args.put, owner, now)
    if not lines:
        print("nothing to change")
        return 0
    # Same validation and encoding the Categories API applies before it saves.
    body = snapshot_to_json(snapshot_from_json(snapshot))
    print(("applying" if args.apply else "dry run, would apply") + f" to {store_path}:")
    for line in lines:
        print("  " + line)
    if not args.apply:
        return 0
    assert store is not None
    base = active.revision if active else None
    row = store.create_candidate(body, base, owner, "Seed starter categories")
    activated = store.activate(row.revision, base)
    print(f"activated revision {activated.revision} (was {base})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

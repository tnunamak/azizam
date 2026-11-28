"""One-time move of category audiences onto access plans (admin schema 21 -> 22).

Until now a category carried its own audience, ``visible_to``: absent or null for everyone, an empty list for the
owner only, or the ids of the access plans that may use it. Access plans now list the categories they include
(docs/design/CATEGORIES_AS_LABELS.md). ``init_db`` runs this once, after the schema is brought up to date:

- Each plan's ``categories`` becomes the sorted ids of the active categories whose audience reached it. A category
  with no ``visible_to`` (or null) reaches every plan; a category that named the plan reaches it; a category with
  ``visible_to == []`` reaches no plan.
- It never writes ``"all"``. Including every category, now and later, is the owner's choice to make in the plan editor.
- The plan rows change in one immediate transaction on the admin store's connection, and the step is recorded in that
  same transaction. A second start finds the step and does nothing.

Every (person plan, app plan, category) decision is the same before and after: a category is usable only when both
plans reached it under the old audiences, which is exactly what both plans listing it now means.

If the category store exists but cannot be read, the migration raises ``MigrationError`` and the store does not open.
Writing plans without the audiences they came from would silently change who can use what.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from gateway.category_store import CategoryStore, NoActiveSnapshotError

# The category store sits beside admin.db (proxy.py: ADMIN_DB_PATH.parent / "categories.db").
CATEGORY_STORE_NAME = "categories.db"
STEP = "access-plan-categories"


class MigrationError(RuntimeError):
    """The category audiences cannot be read, so no plan is migrated. The message names no stored values."""


def active_categories(path: Path) -> Mapping[str, Any]:
    """The active snapshot's category documents as stored, ``visible_to`` included.

    Empty when there is no category store or no active snapshot: then no category exists and every plan reaches none.
    The store is not created here; the constructor would create a missing file.
    """
    if not path.exists():
        return {}
    try:
        body = CategoryStore(path).get_active().body
    except NoActiveSnapshotError:
        return {}
    except Exception as exc:  # noqa: BLE001 - any read failure stops the migration; error text may carry paths
        raise MigrationError(f"category store unreadable ({type(exc).__name__})") from exc
    categories = body.get("categories") if isinstance(body, dict) else None
    if not isinstance(categories, dict):
        raise MigrationError("category store snapshot has no categories object")
    return categories


def categories_by_plan(categories: Mapping[str, Any], plan_ids: Iterable[str]) -> dict[str, list[str]]:
    """Each plan's category ids under the old audiences: the ids of the categories that reached it, sorted."""
    reached: dict[str, list[str]] = {plan_id: [] for plan_id in plan_ids}
    for category_id, document in categories.items():
        visible_to = document.get("visible_to") if isinstance(document, Mapping) else None
        if visible_to is not None and not isinstance(visible_to, list):
            raise MigrationError("category visibility is not a list of access plan ids")
        for plan_id in reached:
            if visible_to is None or plan_id in visible_to:
                reached[plan_id].append(category_id)
    return {plan_id: sorted(ids) for plan_id, ids in reached.items()}


def migrate(conn: sqlite3.Connection, categories_path: Path, *, now: int) -> bool:
    """Run the step once on the admin store's connection, stamping ``updated_at`` with ``now``. Returns True when it ran."""
    if conn.execute("SELECT 1 FROM schema_migration_steps WHERE name = ?", (STEP,)).fetchone():
        return False
    categories = active_categories(categories_path)  # Read before the write transaction opens.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute("SELECT id, plan_json FROM access_plans").fetchall()
        reached = categories_by_plan(categories, (plan_id for plan_id, _ in rows))
        for plan_id, plan_json in rows:
            document = json.loads(plan_json)
            document["categories"] = reached[plan_id]  # Never "all": see the module docstring.
            conn.execute("UPDATE access_plans SET plan_json = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(document, ensure_ascii=False, sort_keys=True), now, plan_id))
        conn.execute("INSERT OR IGNORE INTO schema_migration_steps(name) VALUES (?)", (STEP,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return True

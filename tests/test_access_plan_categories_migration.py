"""Category audiences move onto access plans once (gateway/access_plan_categories.py).

The oracle is the audience rule the gate applied before access plans chose categories (``category_refuses`` in
gateway/category_enforcement.py at f1e7dcdef). A category with no ``visible_to`` refused nothing; otherwise a request
was refused unless its person plan and its app plan were both listed. A request with no person or app plan is denied
by the access check itself (gateway/policy.py), so the comparison covers present plans only.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path

import pytest

from gateway import admin_store
from gateway.access_plan_categories import STEP, MigrationError, categories_by_plan
from gateway.access_plans import plan_from_document
from gateway.category_enforcement import refusing_categories
from gateway.category_store import CategoryStore
from tests.support.access import plan_document


def _old_refuses(visible_to: list[str] | None, person_plan_id: str | None, app_plan_id: str | None) -> bool:
    """The audience rule before access plans chose categories (gateway/category_enforcement.py at f1e7dcdef)."""
    if visible_to is None:
        return False
    plans = {person_plan_id, app_plan_id}
    return None in plans or not plans <= set(visible_to)


def _stored(path: Path, plan_id: str) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        (raw,) = conn.execute("SELECT plan_json FROM access_plans WHERE id = ?", (plan_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(raw)


def _forget_step(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM schema_migration_steps WHERE name = ?", (STEP,))
        conn.commit()
    finally:
        conn.close()


def _owner_edits_categories(path: Path, plan_id: str, categories: list[str]) -> None:
    document = _stored(path, plan_id)
    document["categories"] = categories
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("UPDATE access_plans SET plan_json = ? WHERE id = ?",
                     (json.dumps(document, sort_keys=True), plan_id))
        conn.commit()
    finally:
        conn.close()


def _write_old_rules(path: Path, audiences: dict[str, list[str] | None]) -> None:
    """An active snapshot in the shape the previous release stored, ``visible_to`` included.

    A null audience is stored as an absent key for half the categories and as null for the rest: both read as everyone.
    """
    categories = {}
    for index, (key, visible_to) in enumerate(audiences.items()):
        document: dict = {"id": key, "label": key, "kind": "set", "values": ["included"],
                          "applies_to": ["release", "route", "workflow"], "description": ""}
        if visible_to is not None or index % 2:
            document["visible_to"] = visible_to
        categories[key] = document
    body = {"id": "rules", "categories": categories, "people": {}, "apps": {}, "services": {}}
    store = CategoryStore(path)
    row = store.create_candidate(body, None, "owner-1", "test rules")
    store.activate(row.revision, None)


def _three_plans(path: Path) -> tuple[str, str, str]:
    family, teens, guests = (
        admin_store.create_access_plan(plan_document(name), path=path)["id"] for name in ("Family", "Teens", "Guests")
    )
    return family, teens, guests


def test_each_audience_reaches_the_plans_it_named_and_no_plan_gets_all():
    reached = categories_by_plan(
        {"open": {}, "null": {"visible_to": None}, "owner": {"visible_to": []}, "family": {"visible_to": ["plan_a"]}},
        ["plan_a", "plan_b"],
    )

    assert reached == {"plan_a": ["family", "null", "open"], "plan_b": ["null", "open"]}


def test_the_migrated_plans_list_exactly_the_categories_that_reached_them(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    family, teens, guests = _three_plans(path)
    _write_old_rules(tmp_path / "categories.db", {
        "open": None, "owner_only": [], "family_only": [family], "family_and_teens": [family, teens],
    })
    _forget_step(path)

    admin_store.init_db(path)

    assert _stored(path, family)["categories"] == ["family_and_teens", "family_only", "open"]
    assert _stored(path, teens)["categories"] == ["family_and_teens", "open"]
    assert _stored(path, guests)["categories"] == ["open"]


def test_categories_whose_ids_differ_only_by_a_space_stay_two_categories(tmp_path):
    """Category ids are opaque. "adult " (everyone) and "adult" (the owner only) are two categories, so the migrated
    family plan must include "adult " exactly and must not read back as including the owner-only "adult"."""
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    family = _three_plans(path)[0]
    _write_old_rules(tmp_path / "categories.db", {"adult ": None, "adult": []})
    _forget_step(path)

    admin_store.init_db(path)

    family_plan = plan_from_document(_stored(path, family), plan_id=family)
    assert family_plan.categories == frozenset({"adult "})
    assert refusing_categories(["adult"], family_plan, family_plan) == ("adult",)


def test_the_migrated_plans_decide_every_request_as_the_old_audiences_did(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    family, teens, guests = _three_plans(path)
    audiences = {"open": None, "owner_only": [], "family_only": [family], "family_and_teens": [family, teens]}
    _write_old_rules(tmp_path / "categories.db", audiences)
    _forget_step(path)
    admin_store.init_db(path)
    plans = {plan_id: plan_from_document({**_stored(path, plan_id), "id": plan_id})
             for plan_id in (family, teens, guests)}

    for category, visible_to in audiences.items():
        for person, app in itertools.product(plans, plans):
            refused = category in refusing_categories((category,), plans[person], plans[app])
            assert refused == _old_refuses(visible_to, person, app), (category, person, app)
    # The oracle is not vacuous: the family-only category admits its own plan and refuses the teens' plan.
    assert "family_only" not in refusing_categories(("family_only",), plans[family], plans[family])
    assert "family_only" in refusing_categories(("family_only",), plans[family], plans[teens])


def test_the_step_runs_once_so_an_owner_edit_made_afterwards_is_kept(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    family, _, _ = _three_plans(path)
    _write_old_rules(tmp_path / "categories.db", {"open": None})
    _forget_step(path)
    admin_store.init_db(path)
    _owner_edits_categories(path, family, [])

    admin_store.init_db(path)

    assert _stored(path, family)["categories"] == []


def test_a_category_store_that_appears_after_the_step_does_not_reach_existing_plans(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    family, _, _ = _three_plans(path)
    _forget_step(path)
    admin_store.init_db(path)  # The step runs over the existing plans with no category store: each keeps an empty list.

    _write_old_rules(tmp_path / "categories.db", {"open": None})
    admin_store.init_db(path)  # The step has run, so a store that appears later does not reach them.

    assert _stored(path, family)["categories"] == []


def test_unreadable_audiences_stop_the_start_and_the_step_runs_at_the_next_start(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    family, _, _ = _three_plans(path)
    _forget_step(path)
    (tmp_path / "categories.db").write_bytes(b"not a sqlite database" * 64)
    before = _stored(path, family)["categories"]

    with pytest.raises(MigrationError):
        admin_store.init_db(path)
    assert _stored(path, family)["categories"] == before  # The failed start wrote nothing.

    (tmp_path / "categories.db").unlink()
    _write_old_rules(tmp_path / "categories.db", {"open": None})
    admin_store.init_db(path)

    assert _stored(path, family)["categories"] == ["open"]
